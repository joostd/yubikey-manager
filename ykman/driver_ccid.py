# Copyright (c) 2015 Yubico AB
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


import struct
import subprocess
import time
from smartcard import System, Exceptions
from .driver import AbstractDriver
from .util import Mode, CAPABILITY, TRANSPORT
from .yubicommon.compat import byte2int, int2byte

SW_OK = 0x9000

INS_SELECT = 0xa4
INS_YK4_CAPABILITIES = 0x1d

INS_YK2_REQ = 0x01
SLOT_DEVICE_SERIAL = 0x10
SLOT_DEVICE_CONFIG = 0x11

INS_NEO_TEST = 0x16

OTP_AID = b'\xa0\x00\x00\x05\x27\x20\x01'
MGR_AID = b'\xa0\x00\x00\x05\x27\x47\x11\x17'
OPGP_AID = b'\xd2\x76\x00\x01\x24\x01'

KNOWN_APPLETS = {
    OTP_AID: CAPABILITY.OTP,
    b'\xa0\x00\x00\x06\x47\x2f\x00\x01': CAPABILITY.U2F,  # Official
    b'\xa0\x00\x00\x05\x27\x10\x02': CAPABILITY.U2F,  # Yubico - No longer used
    b'\xa0\x00\x00\x03\x08': CAPABILITY.PIV,
    OPGP_AID: CAPABILITY.OPGP,
    b'\xa0\x00\x00\x05\x27\x21\x01': CAPABILITY.OATH
}


class CCIDDriver(AbstractDriver):
    """
    Pyscard based CCID driver
    """
    transport = TRANSPORT.CCID

    def __init__(self, connection, name=''):
        self._conn = connection
        self._mode = Mode(sum(t for t in TRANSPORT if t.name in name))
        if ' NEO ' in name:  # At least 3.0.0
            self._version = (3, 0, 0)
        elif ' 4 ' in name:  # At least 4.1.0 if CCID is available.
            self._version = (4, 1, 0)
        self._read_version()  # Overwrite with exact version, if possible.

    def _read_version(self):
        s, sw = self.send_apdu(0, INS_SELECT, 4, 0, OTP_AID)
        if sw == SW_OK:
            self._version = tuple(byte2int(c) for c in s[:3])
            serial, sw = self.send_apdu(0, INS_YK2_REQ, SLOT_DEVICE_SERIAL, 0)
            if len(serial) == 4 and sw == SW_OK:
                self._serial = struct.unpack('>I', serial)[0]

    def read_capabilities(self):
        if self.version == (4, 2, 4):  # 4.2.4 doesn't report correctly.
            return b'\x03\x01\x01\x3f'
        _, sw = self.send_apdu(0, INS_SELECT, 4, 0, MGR_AID)
        if sw != SW_OK:
            return b''
        capa, sw = self.send_apdu(0, INS_YK4_CAPABILITIES, 0, 0)
        return capa

    def probe_capabilities_support(self):
        capa = CAPABILITY.CCID
        for aid, code in KNOWN_APPLETS.items():
            _, sw = self.send_apdu(0, INS_SELECT, 4, 0, aid)
            if sw == SW_OK:
                capa |= code
        return capa

    def send_apdu(self, cl, ins, p1, p2, data=b''):
        header = [cl, ins, p1, p2, len(data)]
        body = [byte2int(c) for c in data]
        resp, sw1, sw2 = self._conn.transmit(header + body)
        return b''.join([int2byte(c) for c in resp]), sw1 << 8 | sw2

    def set_mode(self, mode_code, cr_timeout=0, autoeject_time=0):
        for aid, ins in [(OTP_AID, INS_YK2_REQ), (MGR_AID, INS_NEO_TEST)]:
            _, sw = self.send_apdu(0, INS_SELECT, 4, 0, aid)
            if sw == SW_OK:
                data = struct.pack('BBH', mode_code, cr_timeout, autoeject_time)
                _, sw = self.send_apdu(0, ins, SLOT_DEVICE_CONFIG, 0, data)
                if sw == SW_OK:
                    return
                else:
                    raise Exception('Setting mode failed!')
        raise Exception('Selecting applet failed!')

    def __del__(self):
        try:
            self._conn.disconnect()
        except:
            pass  # Ignore


def kill_scdaemon():
    try:
        # Works for Windows.
        from win32com.client import GetObject
        from win32api import OpenProcess, CloseHandle, TerminateProcess
        WMI = GetObject('winmgmts:')
        ps = WMI.InstancesOf('Win32_Process')
        for p in ps:
            if p.Properties_('Name').Value == 'scdaemon.exe':
                pid = p.Properties_('ProcessID').Value
                print("Stopping scdaemon...")
                handle = OpenProcess(1, False, pid)
                TerminateProcess(handle, -1)
                CloseHandle(handle)
                time.sleep(0.1)
    except ImportError:
        # Works for Linux and OS X.
        pids = subprocess.check_output(
            "ps ax | grep scdaemon | grep -v grep | awk '{ print $1 }'",
            shell=True).strip()
        if pids:
            for pid in pids.split():
                print("Stopping scdaemon...")
                subprocess.call(['kill', '-9', pid])
            time.sleep(0.1)


def open_device():
    for reader in System.readers():
        if reader.name.lower().startswith('yubico yubikey'):
            try:
                conn = reader.createConnection()
                conn.connect()
            except Exceptions.CardConnectionException as e:
                if 'Sharing violation' in str(e):
                    kill_scdaemon()
                    return open_device()
                raise
            return CCIDDriver(conn, reader.name)
