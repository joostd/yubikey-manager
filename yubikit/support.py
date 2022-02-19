# Copyright (c) 2015-2022 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from .core import (
    TRANSPORT,
    YUBIKEY,
    PID,
    Version,
    Connection,
    NotSupportedError,
    ApplicationNotAvailableError,
)
from .core.otp import OtpConnection, CommandRejectedError
from .core.fido import FidoConnection
from .core.smartcard import (
    AID,
    SmartCardConnection,
    SmartCardProtocol,
)
from .management import (
    ManagementSession,
    DeviceInfo,
    DeviceConfig,
    USB_INTERFACE,
    CAPABILITY,
    FORM_FACTOR,
    DEVICE_FLAG,
)
from .yubiotp import YubiOtpSession

from time import sleep
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)


def is_fips_version(version: Version) -> bool:
    """True if a given firmware version indicates a YubiKey (4) FIPS"""
    return (4, 4, 0) <= version < (4, 5, 0)


def _otp_read_data(conn) -> Tuple[Version, Optional[int]]:
    otp = YubiOtpSession(conn)
    version = otp.version
    serial: Optional[int] = None
    try:
        serial = otp.get_serial()
    except Exception:
        logger.debug("Unable to read serial over OTP, no serial", exc_info=True)
    return version, serial


AID_U2F_YUBICO = b"\xa0\x00\x00\x05\x27\x10\x02"  # Old U2F AID

SCAN_APPLETS = {
    # AID.OTP: CAPABILITY.OTP,  # NB: OTP will be checked elsewhere
    AID.FIDO: CAPABILITY.U2F,
    AID_U2F_YUBICO: CAPABILITY.U2F,
    AID.PIV: CAPABILITY.PIV,
    AID.OPENPGP: CAPABILITY.OPENPGP,
    AID.OATH: CAPABILITY.OATH,
}

BASE_NEO_APPS = CAPABILITY.OTP | CAPABILITY.OATH | CAPABILITY.PIV | CAPABILITY.OPENPGP


def _read_info_ccid(conn, key_type, interfaces):
    version: Optional[Version] = None
    try:
        mgmt = ManagementSession(conn)
        version = mgmt.version
        try:
            return mgmt.read_device_info()
        except NotSupportedError:
            # Workaround to "de-select" the Management Applet needed for NEO
            conn.send_and_receive(b"\xa4\x04\x00\x08")
    except ApplicationNotAvailableError:
        logger.debug("Couldn't select Management application, use fallback")

    # Synthesize data
    capabilities = CAPABILITY(0)

    # Try to read serial (and version if needed) from OTP application
    try:
        otp_version, serial = _otp_read_data(conn)
        capabilities |= CAPABILITY.OTP
        if version is None:
            version = otp_version
    except ApplicationNotAvailableError:
        logger.debug("Couldn't select OTP application, serial unknown")
        serial = None

    if version is None:
        logger.debug("Firmware version unknown, using 3.0.0 as a baseline")
        version = Version(3, 0, 0)  # Guess, no way to know

    # Scan for remaining capabilities
    logger.debug("Scan for available applications...")
    protocol = SmartCardProtocol(conn)
    for aid, code in SCAN_APPLETS.items():
        try:
            protocol.select(aid)
            capabilities |= code
            logger.debug("Found applet: aid: %s, capability: %s", aid, code)
        except ApplicationNotAvailableError:
            logger.debug("Missing applet: aid: %s, capability: %s", aid, code)
        except Exception:
            logger.exception(
                "Error selecting aid: %s, capability: %s",
                aid,
                code,
            )

    # Assume U2F on devices >= 3.3.0
    if USB_INTERFACE.FIDO in interfaces or version >= (3, 3, 0):
        capabilities |= CAPABILITY.U2F

    return DeviceInfo(
        config=DeviceConfig(
            enabled_capabilities={},  # Populated later
            auto_eject_timeout=0,
            challenge_response_timeout=0,
            device_flags=DEVICE_FLAG(0),
        ),
        serial=serial,
        version=version,
        form_factor=FORM_FACTOR.UNKNOWN,
        supported_capabilities={
            TRANSPORT.USB: capabilities,
            TRANSPORT.NFC: capabilities,
        },
        is_locked=False,
    )


def _read_info_otp(conn, key_type, interfaces):
    otp = None
    serial = None

    try:
        mgmt = ManagementSession(conn)
    except ApplicationNotAvailableError:
        otp = YubiOtpSession(conn)

    # Retry during potential reclaim timeout period (~3s).
    for _ in range(8):
        try:
            if otp is None:
                try:
                    return mgmt.read_device_info()  # Rejected while reclaim
                except NotSupportedError:
                    otp = YubiOtpSession(conn)
            serial = otp.get_serial()  # Rejected if reclaim (or not API_SERIAL_VISIBLE)
            break
        except CommandRejectedError:
            if otp and interfaces == USB_INTERFACE.OTP:
                break  # Can't be reclaim with only one interface
            logger.debug("Potential reclaim, sleep...", exc_info=True)
            sleep(0.5)  # Potential reclaim
    else:
        otp = YubiOtpSession(conn)

    # Synthesize info
    logger.debug("Unable to get info via Management application, use fallback")

    version = otp.version
    if key_type == YUBIKEY.NEO:
        usb_supported = BASE_NEO_APPS
        if USB_INTERFACE.FIDO in interfaces or version >= (3, 3, 0):
            usb_supported |= CAPABILITY.U2F
        capabilities = {
            TRANSPORT.USB: usb_supported,
            TRANSPORT.NFC: usb_supported,
        }
    elif key_type == YUBIKEY.YKP:
        capabilities = {
            TRANSPORT.USB: CAPABILITY.OTP | CAPABILITY.U2F,
        }
    else:
        capabilities = {
            TRANSPORT.USB: CAPABILITY.OTP,
        }

    return DeviceInfo(
        config=DeviceConfig(
            enabled_capabilities={},  # Populated later
            auto_eject_timeout=0,
            challenge_response_timeout=0,
            device_flags=DEVICE_FLAG(0),
        ),
        serial=serial,
        version=version,
        form_factor=FORM_FACTOR.UNKNOWN,
        supported_capabilities=capabilities.copy(),
        is_locked=False,
    )


def _read_info_ctap(conn, key_type, interfaces):
    try:
        mgmt = ManagementSession(conn)
        return mgmt.read_device_info()
    except Exception:  # SKY 1, NEO, or YKP
        logger.debug("Unable to get info via Management application, use fallback")

        # Best guess version
        if key_type == YUBIKEY.YKP:
            version = Version(4, 0, 0)
        else:
            version = Version(3, 0, 0)

        supported_apps = {TRANSPORT.USB: CAPABILITY.U2F}
        if key_type == YUBIKEY.NEO:
            supported_apps[TRANSPORT.USB] |= BASE_NEO_APPS
            supported_apps[TRANSPORT.NFC] = supported_apps[TRANSPORT.USB]

        return DeviceInfo(
            config=DeviceConfig(
                enabled_capabilities={},  # Populated later
                auto_eject_timeout=0,
                challenge_response_timeout=0,
                device_flags=DEVICE_FLAG(0),
            ),
            serial=None,
            version=version,
            form_factor=FORM_FACTOR.USB_A_KEYCHAIN,
            supported_capabilities=supported_apps,
            is_locked=False,
        )


def read_info(pid: Optional[PID], conn: Connection) -> DeviceInfo:
    """Read out a DeviceInfo object from a YubiKey, or attempt to synthesize one."""

    logger.debug(f"Attempting to read device info, using {type(conn).__name__}")
    if pid:
        key_type: Optional[YUBIKEY] = pid.yubikey_type
        interfaces = pid.usb_interfaces
    else:  # No PID for NFC connections
        key_type = None
        interfaces = USB_INTERFACE(0)

    if isinstance(conn, SmartCardConnection):
        info = _read_info_ccid(conn, key_type, interfaces)
    elif isinstance(conn, OtpConnection):
        info = _read_info_otp(conn, key_type, interfaces)
    elif isinstance(conn, FidoConnection):
        info = _read_info_ctap(conn, key_type, interfaces)
    else:
        raise TypeError("Invalid connection type")

    logger.debug("Read info: %s", info)

    # Set usb_enabled if missing (pre YubiKey 5)
    if (
        info.has_transport(TRANSPORT.USB)
        and TRANSPORT.USB not in info.config.enabled_capabilities
    ):
        usb_enabled = info.supported_capabilities[TRANSPORT.USB]
        if usb_enabled == (CAPABILITY.OTP | CAPABILITY.U2F | USB_INTERFACE.CCID):
            # YubiKey Edge, hide unusable CCID interface from supported
            # usb_enabled = CAPABILITY.OTP | CAPABILITY.U2F
            info.supported_capabilities = {
                TRANSPORT.USB: CAPABILITY.OTP | CAPABILITY.U2F
            }

        if USB_INTERFACE.OTP not in interfaces:
            usb_enabled &= ~CAPABILITY.OTP
        if USB_INTERFACE.FIDO not in interfaces:
            usb_enabled &= ~(CAPABILITY.U2F | CAPABILITY.FIDO2)
        if USB_INTERFACE.CCID not in interfaces:
            usb_enabled &= ~(
                USB_INTERFACE.CCID
                | CAPABILITY.OATH
                | CAPABILITY.OPENPGP
                | CAPABILITY.PIV
            )

        info.config.enabled_capabilities[TRANSPORT.USB] = usb_enabled

    # YK4-based FIPS version
    if is_fips_version(info.version):
        info.is_fips = True

    # Set nfc_enabled if missing (pre YubiKey 5)
    if (
        info.has_transport(TRANSPORT.NFC)
        and TRANSPORT.NFC not in info.config.enabled_capabilities
    ):
        info.config.enabled_capabilities[TRANSPORT.NFC] = info.supported_capabilities[
            TRANSPORT.NFC
        ]

    # Workaround for invalid configurations.
    if info.version >= (4, 0, 0):
        if info.form_factor in (
            FORM_FACTOR.USB_A_NANO,
            FORM_FACTOR.USB_C_NANO,
            FORM_FACTOR.USB_C_LIGHTNING,
        ) or (
            info.form_factor is FORM_FACTOR.USB_C_KEYCHAIN and info.version < (5, 2, 4)
        ):
            # Known not to have NFC
            info.supported_capabilities.pop(TRANSPORT.NFC, None)
            info.config.enabled_capabilities.pop(TRANSPORT.NFC, None)

    logger.debug("Device info, after tweaks: %s", info)
    return info


def _fido_only(capabilities):
    return capabilities & ~(CAPABILITY.U2F | CAPABILITY.FIDO2) == 0


def _is_preview(version):
    _PREVIEW_RANGES = (
        ((5, 0, 0), (5, 1, 0)),
        ((5, 2, 0), (5, 2, 3)),
        ((5, 5, 0), (5, 5, 2)),
    )
    for start, end in _PREVIEW_RANGES:
        if start <= version < end:
            return True
    return False


def get_name(info: DeviceInfo, key_type: Optional[YUBIKEY]) -> str:
    """Determine the product name of a YubiKey"""
    usb_supported = info.supported_capabilities[TRANSPORT.USB]
    if not key_type:
        if info.serial is None and _fido_only(usb_supported):
            key_type = YUBIKEY.SKY
        elif info.version[0] == 3:
            key_type = YUBIKEY.NEO
        else:
            key_type = YUBIKEY.YK4

    device_name = key_type.value

    if key_type == YUBIKEY.SKY:
        if CAPABILITY.FIDO2 not in usb_supported:
            device_name = "FIDO U2F Security Key"  # SKY 1
        if info.has_transport(TRANSPORT.NFC):
            device_name = "Security Key NFC"
    elif key_type == YUBIKEY.YK4:
        major_version = info.version[0]
        if major_version < 4:
            if info.version[0] == 0:
                return f"YubiKey ({info.version})"
            else:
                return "YubiKey"
        elif major_version == 4:
            if is_fips_version(info.version):  # YK4 FIPS
                device_name = "YubiKey FIPS"
            elif usb_supported == CAPABILITY.OTP | CAPABILITY.U2F:
                device_name = "YubiKey Edge"
            else:
                device_name = "YubiKey 4"

        if _is_preview(info.version):
            device_name = "YubiKey Preview"
        elif info.version >= (5, 1, 0):
            is_nano = info.form_factor in (
                FORM_FACTOR.USB_A_NANO,
                FORM_FACTOR.USB_C_NANO,
            )
            is_bio = info.form_factor in (FORM_FACTOR.USB_A_BIO, FORM_FACTOR.USB_C_BIO)
            is_c = info.form_factor in (  # Does NOT include Ci
                FORM_FACTOR.USB_C_KEYCHAIN,
                FORM_FACTOR.USB_C_NANO,
                FORM_FACTOR.USB_C_BIO,
            )

            if info.is_sky:
                name_parts = ["Security Key"]
            else:
                name_parts = ["YubiKey"]
                if not is_bio:
                    name_parts.append("5")
            if is_c:
                name_parts.append("C")
            elif info.form_factor == FORM_FACTOR.USB_C_LIGHTNING:
                name_parts.append("Ci")
            if is_nano:
                name_parts.append("Nano")
            if info.has_transport(TRANSPORT.NFC):
                name_parts.append("NFC")
            elif info.form_factor == FORM_FACTOR.USB_A_KEYCHAIN:
                name_parts.append("A")  # Only for non-NFC A Keychain.
            if is_bio:
                name_parts.append("Bio")
                if _fido_only(usb_supported):
                    name_parts.append("- FIDO Edition")
            if info.is_fips:
                name_parts.append("FIPS")
            device_name = " ".join(name_parts).replace("5 C", "5C").replace("5 A", "5A")

    return device_name
