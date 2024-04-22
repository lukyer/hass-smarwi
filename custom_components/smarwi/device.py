# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2024 Jakub Jirutka <jakub@jirutka.cz>
from __future__ import annotations

from enum import IntEnum, StrEnum
from functools import cached_property
import socket
import struct
from typing import TYPE_CHECKING, Any, cast  # pyright:ignore[reportAny]
from typing_extensions import override

import aiohttp
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send  # pyright:ignore[reportUnknownVariableType]
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_REMOTE_ID,
    DEVICE_INFO_MANUFACTURER,
    DEVICE_INFO_MODEL,
    DOMAIN,
    HTTP_TIMEOUT,
    LOGGER,
    signal_device_update,
)

if TYPE_CHECKING:
    from functools import cached_property
else:
    from homeassistant.backports.functools import cached_property

__all__ = ["FinetuneSetting", "SmarwiDevice", "SmarwiDeviceProp", "StateCode"]


class SmarwiDeviceProp(StrEnum):
    """Updatable properties of SmarwiDevice."""

    # status properties
    NAME = "cid"
    RIDGE_FIXED = "fix"
    FW_VERSION = "fw"
    IP_ADDRESS = "ip"
    CLOSED = "pos"
    RIDGE_INSIDE = "ro"
    RSSI = "rssi"
    STATE_CODE = "s"
    # others
    AVAILABLE = "available"
    FINETUNE_SETTINGS = "finetune_settings"

    @override
    def __hash__(self) -> int:
        return hash(self.name)


class FinetuneSetting(StrEnum):
    """SMARWI finetune setting keys."""

    MAX_OPEN_POSITION = "vpct"  # Maximum open position
    MOVE_SPEED = "ospd"  # Movement speed
    FRAME_SPEED = "ofspd"  # Near frame speed
    MOVE_POWER = "orpwr"  # Movement power
    FRAME_POWER = "ofpwr"  # Near frame power
    CLOSED_HOLD_POWER = "ohcpwr"  # Closed holding power
    OPENED_HOLD_POWER = "ohopwr"  # Opened holding power
    CLOSED_POSITION = "hdist"  # Window closed position finetune
    LOCK_ERR_TRIGGER = "lwid"  # "Window locked" error trigger
    CALIBRATED_DISTANCE = "cfdist"  # Calibrated distance


class StateCode(IntEnum):
    """SMARWI state codes."""

    UNKNOWN = 0  # Fallback for unknown code
    ERR_WINDOW_LOCKED = 10  # Window is locked to frame
    ERR_MOVE_TIMEOUT = 20  # Operation move to frame sensor from ventilation timeout
    ERR_WINDOW_HORIZ = 30  # Indicates, that window is opened in horizontal position
    CALIBRATION_1 = 110  # Calibration - after clicking on Continue (window is opening)
    CALIBRATION_2 = 120  # Calibration - passing the frame sensor (window is opening)
    CALIBRATION_3 = 130  # Calibration - after clicking on Finish (window is closing)
    OPENING_START = 200  # Moving to frame sensor position within opening phase.
    OPENING = 210  # Opening phase lasts until target ventilation position is reached
    REOPEN_START = 212  # Reopen in executed when open operation is invoked while window position is between frame sensor and ventilation distance
    REOPEN_PHASE = 214  # Window reached frame sensor
    REOPEN_FINAL = 216  # Final phase of reopen operation, window moved to target ventilation position
    CLOSING_START = 220  # Moving to frame sensor position within closing phase
    CLOSING = 230  # Closing phase lasts until target closed position is reached
    CLOSING_NICE = 231  # Closing step by step until obstacle detected?
    RECLOSE_START = 232  # Re-closing starts when close operation in invoked while window position is between frame and frame sensor
    RECLOSE_PHASE = 234  # Window moved after frame sensor
    IDLE = 250

    @override
    @classmethod
    def _missing_(cls, value: object) -> StateCode:
        return cls.UNKNOWN

    def is_closing(self) -> bool:
        """Return True if the window is closing."""
        return 220 <= self.value < 240

    def is_error(self) -> bool:
        """Return True if the state indicate an error (or in calibration)."""
        return self.value < 200

    def is_idle(self) -> bool:
        """Return True if the window is in idle."""
        return self == StateCode.IDLE

    def is_moving(self) -> bool:
        """Return True if the window is moving."""
        return self.is_opening() or self.is_closing()

    def is_near_frame(self) -> bool:
        """Return True if the window is between the frame and the frame sensor."""
        # TODO: add more?
        return self in (
            StateCode.OPENING_START,
            StateCode.REOPEN_PHASE,
            StateCode.CLOSING,
            StateCode.CLOSING_NICE,
        )

    def is_opening(self) -> bool:
        """Return True if the window is opening."""
        return 200 <= self.value < 220


class SmarwiDevice:
    """This class handles communication with a single SMARWI device."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize. Run `.async_init()` afterwards."""
        super().__init__()
        config: dict[str, Any] = entry.data  # pyright:ignore[reportAny]

        self._hass = hass
        self._config_entry = entry
        self._id = device_id

        self._remote_id: str = config[CONF_REMOTE_ID]
        self._base_topic = f"ion/{self._remote_id}/%{device_id}"
        self._available = False
        self._status: dict[SmarwiDeviceProp, str] = {}
        self._finetune_settings = FinetuneSettings(self)

    @cached_property
    def id(self) -> str:
        """Return device ID (serial)."""
        return self._id

    @cached_property
    def basic_device_info(self) -> DeviceInfo:
        """Return immutable part of the DeviceInfo."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.id)},
            manufacturer=DEVICE_INFO_MANUFACTURER,
            model=DEVICE_INFO_MODEL,
        )

    @cached_property
    def finetune_settings(self) -> FinetuneSettings:
        """Return Finetune settings."""
        return self._finetune_settings

    @cached_property
    def signal_update(self) -> str:
        """Device specific event to signal a change in the status properties or availability."""
        return signal_device_update(self.id)

    @property
    def available(self) -> bool:
        """Return `True` if the device is available (online)."""
        return self._available

    @property
    def closed(self) -> bool | None:
        """Return `True` if the window is open, `False` if closed."""
        if SmarwiDeviceProp.CLOSED in self._status:
            return self._status[SmarwiDeviceProp.CLOSED] == "c"

    @property
    def fw_version(self) -> str | None:
        """Return the firmware version of the device."""
        return self._status.get(SmarwiDeviceProp.FW_VERSION)

    @property
    def ip_address(self) -> str | None:
        """Return the IPv4 address of the device."""
        if SmarwiDeviceProp.IP_ADDRESS in self._status:
            return parse_ipv4(int(self._status[SmarwiDeviceProp.IP_ADDRESS]))

    @property
    def name(self) -> str | None:
        """Return the device name (CID) configured in the SMARWI settings."""
        return self._status.get(SmarwiDeviceProp.NAME)

    @property
    def ridge_fixed(self) -> bool:
        """Return `True` if the ridge is fixed, i.e. the window cannot be moved by hand."""
        return self._status.get(SmarwiDeviceProp.RIDGE_FIXED) == "1"

    @property
    def ridge_inside(self) -> bool:
        """Return `True` if the ridge is inside the device, i.e. it can be controlled."""
        return self._status.get(SmarwiDeviceProp.RIDGE_INSIDE) == "0"

    @property
    def rssi(self) -> int | None:
        """Return the WiFi signal strength."""
        if SmarwiDeviceProp.RSSI in self._status:
            return int(self._status[SmarwiDeviceProp.RSSI])

    @property
    def state_code(self) -> StateCode:
        """Return the SMARWI state code (parameter `s`)."""
        return StateCode(int(self._status.get(SmarwiDeviceProp.STATE_CODE) or 0))

    async def async_init(self) -> None:
        """Connect to the device."""

        async def handle_status_message(msg: mqtt.ReceiveMessage) -> None:
            status = decode_keyval(cast(str, msg.payload))  # pyright:ignore[reportAny]
            status = {
                SmarwiDeviceProp(k): v
                for k, v in status.items()
                if k in list(SmarwiDeviceProp)
            }
            changed_props = {
                name
                for name in SmarwiDeviceProp
                if self._status.get(name) != status.get(name)
            }
            LOGGER.debug(
                f"Received message from {self._base_topic}/status:\n{msg.payload}\nChanged properties: {[e.name for e in changed_props]}"  # pyright:ignore[reportAny]
            )
            self._status = status

            if self.state_code.is_error():
                LOGGER.error(
                    f"[{self.name}] Reported error: {self.state_code.name} ({self._status.get(SmarwiDeviceProp.STATE_CODE)})"
                )

            if changed_props & {
                SmarwiDeviceProp.NAME,
                SmarwiDeviceProp.IP_ADDRESS,
                SmarwiDeviceProp.FW_VERSION,
            }:
                await self._async_update_device_registry()

            async_dispatcher_send(self._hass, self.signal_update, changed_props)

            if SmarwiDeviceProp.IP_ADDRESS in changed_props:
                LOGGER.info(
                    f"[{self.name}] Fetching Finetune settings from http://{self.ip_address}"
                )
                await self._finetune_settings.async_update()

        async def handle_online_message(msg: mqtt.ReceiveMessage) -> None:
            LOGGER.debug(
                f"Received message from {self._base_topic}/online: {msg.payload}"  # pyright:ignore[reportAny]
            )
            if (available := bool(msg.payload == "1")) != self._available:  # pyright:ignore[reportAny]
                LOGGER.info(
                    f"[{self.name}] SMARWI {self.id} become {'available' if available else 'unavailable'}"
                )
                self._available = available
                async_dispatcher_send(
                    self._hass, self.signal_update, {SmarwiDeviceProp.AVAILABLE}
                )

        self._config_entry.async_on_unload(
            await mqtt.async_subscribe(
                self._hass,
                f"{self._base_topic}/status",
                handle_status_message,
            )
        )
        self._config_entry.async_on_unload(
            await mqtt.async_subscribe(
                self._hass,
                f"{self._base_topic}/online",
                handle_online_message,
            )
        )

    async def async_open(self, position: int = 100) -> None:
        """Open the window; position is between 0 and 100 %."""
        if position > 1:
            LOGGER.info(f"[{self.name}] Opening window")
            await self._async_mqtt_command(f"open;{position}")
        else:
            await self.async_close()

    async def async_close(self) -> None:
        """Close the window."""
        LOGGER.info(f"[{self.name}] Closing window")
        await self._async_mqtt_command("close")

    async def async_stop(self) -> None:
        """Stop the movement action if moving."""
        # If the motor is not moving, "stop" releases the ridge.
        if self.state_code.is_moving():
            LOGGER.info(f"[{self.name}] Stopping movement of window")
            await self._async_mqtt_command("stop")

    async def async_toggle_ridge_fixed(self, state: bool) -> None:
        """Fix (True) or release (False) the ridge."""
        if state:
            if not self.ridge_fixed:
                LOGGER.info(f"[{self.name}] Fixing ridge")
                await self._async_mqtt_command("stop")
        else:
            # If the motor is moving, "stop" stops it.
            if self.ridge_fixed and self.state_code.is_idle():
                LOGGER.info(f"[{self.name}] Releasing ridge")
                await self._async_mqtt_command("stop")

    async def _async_mqtt_command(self, payload: str) -> None:
        LOGGER.debug(f"Sending message to {self._base_topic}/cmd: {payload}")
        await mqtt.async_publish(self._hass, f"{self._base_topic}/cmd", payload)

    async def _async_update_device_registry(self) -> None:
        dev_registry = dr.async_get(self._hass)

        if dev := dev_registry.async_get_device(identifiers={(DOMAIN, self.id)}):
            LOGGER.debug(f"Updating SMARWI {self.id} in device registry")
            dev_registry.async_update_device(
                dev.id,
                configuration_url=f"http://{self.ip_address}",
                name=self.name,
                sw_version=self.fw_version,
                serial_number=self.id,
            )  # pyright:ignore[reportUnusedCallResult]


class FinetuneSettings:
    def __init__(self, device: SmarwiDevice) -> None:
        super().__init__()
        self._device = device
        self._data: dict[str, int] = {}

    def get(self, key: FinetuneSetting) -> int | None:
        return self._data.get(key.value)

    async def async_set(self, key: FinetuneSetting, value: int) -> None:
        if not self._device.ip_address:
            return
        data = self._data.copy()
        data[key.value] = value
        LOGGER.info(
            f"[{self._device.name}] Changing Finetune setting {key.name} from {self._data[key.value]} to {value}"
        )
        try:
            await http_post_data(
                f"http://{self._device.ip_address}/scfa", encode_keyval(data)
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            return LOGGER.error(
                f"[{self._device.name}] Failed to post Finetune settings to http://{self._device.ip_address}: "
                + ("timeout exceeded" if isinstance(err, TimeoutError) else str(err))
            )
        self._data = data  # TODO: race condition?
        await self.async_update()

    async def async_update(self) -> None:
        """Update Finetune settings from SMARWI via HTTP."""
        assert self._device.ip_address is not None, "ip_address is not known yet"

        try:
            data = await http_get(f"http://{self._device.ip_address}/lcfa")
        except (TimeoutError, aiohttp.ClientError) as err:
            return LOGGER.error(
                f"[{self._device.name}] Failed to get Finetune settings from http://{self._device.ip_address}: "
                + ("timeout exceeded" if isinstance(err, TimeoutError) else str(err))
            )

        self._data = {
            k: int(v)
            for k, v in decode_keyval(data).items()
            if k != "cvdist"  # cvdist is ready-only
        }
        async_dispatcher_send(
            self._device._hass,  # pyright:ignore[reportPrivateUsage]
            self._device.signal_update,
            {SmarwiDeviceProp.FINETUNE_SETTINGS},
        )


def parse_ipv4(packed_ip: int) -> str:
    """Parse IPv4 address represented by 32bit number in Little Endian."""
    return socket.inet_ntoa(struct.pack("<L", packed_ip))


def decode_keyval(payload: str) -> dict[str, str]:
    """Parse key:value pairs separated by newlines."""
    return dict(line.split(":", 1) for line in payload.splitlines())


def encode_keyval(data: dict[str, Any]) -> str:
    """Encode the given dict as key:value pairs separated by newlines."""
    return "\n".join(f"{k}:{v}" for k, v in data.items())  # pyright:ignore[reportAny]


async def http_get(url: str) -> str:
    """Send HTTP GET request and return the response body."""
    LOGGER.debug(f"Sending GET {url}")
    async with (
        aiohttp.ClientSession(raise_for_status=True, timeout=HTTP_TIMEOUT) as http,
        http.get(url) as resp,
    ):
        data = await resp.text()
        LOGGER.debug(f"Received response for GET {url}: HTTP {resp.status}\n{data}")
        return data


async def http_post_data(url: str, data: str) -> None:
    """Send HTTP POST request with multipart data as expected by SWARMI."""
    with aiohttp.MultipartWriter("form-data") as mpwriter:
        mpwriter.append(data.encode("ascii")).set_content_disposition(
            "form-data", name="data", filename="/afile"
        )
        LOGGER.debug(f"POST {url}:\n{data}")
        async with aiohttp.ClientSession(
            raise_for_status=True, timeout=HTTP_TIMEOUT
        ) as http:
            await http.post(url, data=mpwriter)  # pyright:ignore[reportUnusedCallResult]
