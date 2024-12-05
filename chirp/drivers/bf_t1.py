# Copyright 2017 Pavel Milanes, CO7WT, <pavelmc@gmail.com>
#
# This driver is a community effort as I don't have the radio on my hands, so
# I was only the director of the orchestra, without the players this may never
# came true, so special thanks to the following hams for their contribution:
# - Henk van der Laan, PA3CQN
#       - Setting Discovery.
#       - Special channels for RELAY and EMERGENCY.
# - Harold Hankins
#       - Memory limits, testing & bug hunting.
# - Dmitry Milkov
#       - Testing & bug hunting.
# - Many others participants in the issue page on Chirp's site.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from chirp import chirp_common, directory, memmap
from chirp import bitwise, errors, util
from chirp.settings import RadioSetting, RadioSettingGroup, \
                RadioSettingValueBoolean, RadioSettingValueList, \
                RadioSettingValueInteger, RadioSettingValueFloat, \
                RadioSettings

import struct
import logging

LOG = logging.getLogger(__name__)

# A note about the memory in these radios
#
# The '9100' OEM software only manipulates the lower 0x0180 bytes on read/write
# operations as we know, the file generated by the OEM software IS NOT an exact
# eeprom image, it's a crude text file with a pseudo csv format
#
# Later investigations by Harold Hankins found that the eeprom extend up to 2k
# consistent with a hardware chip K24C16 a 2k x 8 bit serial eeprom

MEM_SIZE = 0x0800  # 2048 bytes
WRITE_SIZE = 0x0180  # 384 bytes
BLOCK_SIZE = 0x10
ACK_CMD = b"\x06"
MODES = ["NFM", "FM"]
SKIP_VALUES = ["S", ""]
TONES = chirp_common.TONES
DTCS = tuple(sorted(chirp_common.DTCS_CODES + (645,)))

# Special channels
SPECIALS = {
    "EMG": -2,
    "RLY": -1
    }

# Settings vars
TOT_LIST = ["Off"] + ["%s" % x for x in range(30, 210, 30)]
SCAN_TYPE_LIST = ["Time", "Carrier", "Search"]
LANGUAGE_LIST = ["Off", "English", "Chinese"]
TIMER_LIST = ["Off"] + ["%s h" % (x * 0.5) for x in range(1, 17)]
FM_RANGE_LIST = ["76-108", "65-76"]
RELAY_MODE_LIST = ["Off", "RX sync", "TX sync"]
BACKLIGHT_LIST = ["Off", "Key", "On"]
POWER_LIST = ["0.5 Watt", "1.0 Watt"]

# This is a general serial timeout for all serial read functions.
# Practice has show that about 0.07 sec will be enough to cover all radios.
STIMEOUT = 0.07

# this var controls the verbosity in the debug and by default it's low (False)
# make it True and you will to get a very verbose debug.log
debug = False

# #### ID strings #####################################################

# BF-T1 handheld
BFT1_magic = b"\x05PROGRAM"
BFT1_ident = b" BF9100S"


def _clean_buffer(radio):
    """Cleaning the read serial buffer, hard timeout to survive an infinite
    data stream"""

    dump = "1"
    datacount = 0

    try:
        while len(dump) > 0:
            dump = radio.pipe.read(100)
            datacount += len(dump)
            # hard limit to survive a infinite serial data stream
            # 5 times bigger than a normal rx block (20 bytes)
            if datacount > 101:
                seriale = "Please check your serial port selection."
                raise errors.RadioError(seriale)

    except Exception:
        raise errors.RadioError("Unknown error cleaning the serial buffer")


def _rawrecv(radio, amount=0):
    """Raw read from the radio device"""

    # var to hold the data to return
    data = b""

    try:
        if amount == 0:
            data = radio.pipe.read()
        else:
            data = radio.pipe.read(amount)

        # DEBUG
        if debug is True:
            LOG.debug("<== (%d) bytes:\n\n%s" %
                      (len(data), util.hexprint(data)))

        # fail if no data is received
        if len(data) == 0:
            raise errors.RadioError("No data received from radio")

    except:
        raise errors.RadioError("Error reading data from radio")

    return data


def _send(radio, data):
    """Send data to the radio device"""

    try:
        radio.pipe.write(data)

        # DEBUG
        if debug is True:
            LOG.debug("==> (%d) bytes:\n\n%s" %
                      (len(data), util.hexprint(data)))
    except:
        raise errors.RadioError("Error sending data to radio")


def _make_frame(cmd, addr, data=""):
    """Pack the info in the header format"""
    frame = struct.pack(">BHB", ord(cmd), addr, BLOCK_SIZE)

    # add the data if set
    if len(data) != 0:
        frame += data

    return frame


def _recv(radio, addr):
    """Get data from the radio"""

    # Get the full 20 bytes at a time
    # 4 bytes header + 16 bytes of data (BLOCK_SIZE)

    # get the whole block
    block = _rawrecv(radio, BLOCK_SIZE + 4)

    # short answer
    if len(block) < (BLOCK_SIZE + 4):
        raise errors.RadioError("Wrong block length (short) at 0x%04x" % addr)

    # long answer
    if len(block) > (BLOCK_SIZE + 4):
        raise errors.RadioError("Wrong block length (long) at 0x%04x" % addr)

    # header validation
    c, a, l = struct.unpack(">cHB", block[0:4])
    if c != b"W" or a != addr or l != BLOCK_SIZE:
        LOG.debug("Invalid header for block 0x%04x:" % addr)
        LOG.debug("CMD: %s  ADDR: %04x  SIZE: %02x" % (c, a, l))
        raise errors.RadioError("Invalid header for block 0x%04x:" % addr)

    # return the data, 16 bytes of payload
    return block[4:]


def _start_clone_mode(radio, status):
    """Put the radio in clone mode, 3 tries"""

    # cleaning the serial buffer
    _clean_buffer(radio)

    # prep the data to show in the UI
    status.cur = 0
    status.msg = "Identifying the radio..."
    status.max = 3
    radio.status_fn(status)

    try:
        for a in range(0, status.max):
            # Update the UI
            status.cur = a + 1
            radio.status_fn(status)

            # send the magic word
            _send(radio, radio._magic)

            # Now you get a x06 of ACK if all goes well
            ack = _rawrecv(radio, 1)

            if ack == ACK_CMD:
                # DEBUG
                LOG.info("Magic ACK received")
                status.cur = status.max
                radio.status_fn(status)

                return True

        return False

    except errors.RadioError:
        raise
    except Exception as e:
        raise errors.RadioError("Error sending Magic to radio:\n%s" % e)


def _do_ident(radio, status):
    """Put the radio in PROGRAM mode & identify it"""
    #  set the serial discipline (default)
    radio.pipe.baudrate = 9600
    radio.pipe.parity = "N"
    radio.pipe.bytesize = 8
    radio.pipe.stopbits = 1
    radio.pipe.timeout = STIMEOUT

    # open the radio into program mode
    if _start_clone_mode(radio, status) is False:
        raise errors.RadioError("Radio did not enter clone mode, wrong model?")

    # Ok, poke it to get the ident string
    _send(radio, b"\x02")
    ident = _rawrecv(radio, len(radio._id))

    # basic check for the ident
    if len(ident) != len(radio._id):
        raise errors.RadioError("Radio send a odd identification block.")

    # check if ident is OK
    if ident != radio._id:
        LOG.debug("Incorrect model ID, got this:\n\n" + util.hexprint(ident))
        raise errors.RadioError("Radio identification failed.")

    # handshake
    _send(radio, ACK_CMD)
    ack = _rawrecv(radio, 1)

    # checking handshake
    if len(ack) == 1 and ack == ACK_CMD:
        # DEBUG
        LOG.info("ID ACK received")
    else:
        LOG.debug("Radio handshake failed.")
        raise errors.RadioError("Radio handshake failed.")

    # DEBUG
    LOG.info("Positive ident, this is a %s %s" % (radio.VENDOR, radio.MODEL))

    return True


def _download(radio):
    """Get the memory map"""

    # UI progress
    status = chirp_common.Status()

    # put radio in program mode and identify it
    _do_ident(radio, status)

    # reset the progress bar in the UI
    status.max = MEM_SIZE // BLOCK_SIZE
    status.msg = "Cloning from radio..."
    status.cur = 0
    radio.status_fn(status)

    # cleaning the serial buffer
    _clean_buffer(radio)

    data = b""
    for addr in range(0, MEM_SIZE, BLOCK_SIZE):
        # sending the read request
        _send(radio, _make_frame(b"R", addr))

        # read
        d = _recv(radio, addr)

        # aggregate the data
        data += d

        # UI Update
        status.cur = addr // BLOCK_SIZE
        status.msg = "Cloning from radio..."
        radio.status_fn(status)

    # close comms with the radio
    _send(radio, b"\x62")
    # DEBUG
    LOG.info("Close comms cmd sent, radio must reboot now.")

    return data


def _upload(radio):
    """Upload procedure, we only upload to the radio the Writable space"""

    # UI progress
    status = chirp_common.Status()

    # put radio in program mode and identify it
    _do_ident(radio, status)

    # get the data to upload to radio
    data = radio.get_mmap()

    # Reset the UI progress
    status.max = WRITE_SIZE // BLOCK_SIZE
    status.cur = 0
    status.msg = "Cloning to radio..."
    radio.status_fn(status)

    # cleaning the serial buffer
    _clean_buffer(radio)

    # the fun start here, we use WRITE_SIZE instead of the full MEM_SIZE
    for addr in range(0, WRITE_SIZE, BLOCK_SIZE):
        # getting the block of data to send
        d = data[addr:addr + BLOCK_SIZE]

        # build the frame to send
        frame = _make_frame(b"W", addr, d)

        # send the frame
        _send(radio, frame)

        # receiving the response
        ack = _rawrecv(radio, 1)

        # basic check
        if len(ack) != 1:
            raise errors.RadioError("No ACK when writing block 0x%04x" % addr)

        if ack != ACK_CMD:
            raise errors.RadioError("Bad ACK writing block 0x%04x:" % addr)

        # UI Update
        status.cur = addr // BLOCK_SIZE
        status.msg = "Cloning to radio..."
        radio.status_fn(status)

    # close comms with the radio
    _send(radio, b"\x62")
    # DEBUG
    LOG.info("Close comms cmd sent, radio must reboot now.")


def _model_match(cls, data):
    """Match the opened/downloaded image to the correct version"""

    # a reliable fingerprint: the model name at
    rid = data[0x06f8:0x0700]

    if rid == BFT1_ident:
        return True

    return False


def _decode_ranges(low, high):
    """Unpack the data in the ranges zones in the memmap and return
    a tuple with the integer corresponding to the MHz it means"""
    return (int(low) * 100000, int(high) * 100000)


MEM_FORMAT = """

struct channel {
  lbcd rxfreq[4];       // rx freq.
  u8 rxtone;            // x00 = none
                        // x01 - x32 = index of the analog tones
                        // x33 - x9b = index of Digital tones
                        // Digital tone polarity is handled below by
                        // ttondinv & ttondinv settings
  lbcd txoffset[4];     // the difference against RX, direction handled by
                        // offplus & offminus
  u8 txtone;            // Idem to rxtone
  u8 noskip:1,      // if true is included in the scan
     wide:1,        // 1 = Wide, 0 = Narrow
     ttondinv:1,    // if true TX tone is Digital & Inverted
     unA:1,         //
     rtondinv:1,    // if true RX tone is Digital & Inverted
     unB:1,         //
     offplus:1,     // TX = RX + offset
     offminus:1;    // TX = RX - offset
  u8 empty[5];
};

#seekto 0x0000;
struct channel emg;             // channel 0 is Emergent CH
#seekto 0x0010;
struct channel channels[20];    // normal 1-20 mem channels

#seekto 0x0150;     // Settings
struct {
  lbcd vhfl[2];     // VHF low limit
  lbcd vhfh[2];     // VHF high limit
  lbcd uhfl[2];     // UHF low limit
  lbcd uhfh[2];     // UHF high limit
  u8 unk0[8];
  u8 unk1[2];       // start of 0x0160 <=======
  u8 squelch;       // byte: 0-9
  u8 vox;           // byte: 0-9
  u8 timeout;       // tot, 0 off, then 30 sec increments up to 180
  u8 batsave:1,     // battery save 0 = off, 1 = on
     fm_funct:1,    // fm-radio 0=off, 1=on ( off disables fm button on set )
     ste:1,         // squelch tail 0 = off, 1 = on
     blo:1,         // busy lockout 0 = off, 1 = on
     beep:1,        // key beep 0 = off, 1 = on
     lock:1,        // keylock 0 = ff,  = on
     backlight:2;   // backlight 00 = off, 01 = key, 10 = on
  u8 scantype;      // scan type 0 = timed, 1 = carrier, 2 = stop
  u8 channel;       // active channel 1-20, setting it works on upload
  u8 fmrange;       // fm range 1 = low[65-76](ASIA), 0 = high[76-108](AMERICA)
  u8 alarm;         // alarm (count down timer)
                    //    d0 - d16 in half hour increments => off, 0.5 - 8.0 h
  u8 voice;         // voice prompt 0 = off, 1 = English, 2 = Chinese
  u8 volume;        // volume 1-7 as per the radio steps
                    //    set to #FF by original software on upload
                    //    chirp uploads actual value and works.
  u16 fm_vfo;       // the frequency of the fm receiver.
                    //    resulting frequency is 65 + value * 0.1 MHz
                    //    0x145 is then 65 + 325*0.1 = 97.5 MHz
  u8 relaym;        // relay mode, d0 = off, d2 = re-tx, d1 = re-rx
                    //    still a mystery on how it works
  u8 tx_pwr;        // tx pwr 0 = low (0.5W), 1 = high(1.0W)
} settings;

#seekto 0x0170;     // Relay CH
struct channel rly;

"""


@directory.register
class BFT1(chirp_common.CloneModeRadio, chirp_common.ExperimentalRadio):
    """Baofeng BT-F1 radio & possibly alike radios"""
    VENDOR = "Baofeng"
    MODEL = "BF-T1"
    _vhf_range = (130000000, 174000000)
    _uhf_range = (400000000, 520000000)
    _upper = 20
    _magic = BFT1_magic
    _id = BFT1_ident
    _bw_shift = False

    @classmethod
    def get_prompts(cls):
        rp = chirp_common.RadioPrompts()
        rp.experimental = \
            ('This driver is experimental.\n'
             '\n'
             'Please keep a copy of your memories with the original software '
             'if you treasure them, this driver is new and may contain'
             ' bugs.\n'
             '\n'
             '"Emergent CH" & "Relay CH" are implemented via special channels,'
             'be sure to click on the button on the interface to access them.'
             )
        rp.pre_download = _(
            "Follow these instructions to download your info:\n"
            "1 - Turn off your radio\n"
            "2 - Connect your interface cable\n"
            "3 - Turn on your radio\n"
            "4 - Do the download of your radio data\n")
        rp.pre_upload = _(
            "Follow these instructions to upload your info:\n"
            "1 - Turn off your radio\n"
            "2 - Connect your interface cable\n"
            "3 - Turn on your radio\n"
            "4 - Do the upload of your radio data\n")
        return rp

    def get_features(self):
        """Get the radio's features"""

        rf = chirp_common.RadioFeatures()
        rf.valid_special_chans = list(SPECIALS.keys())
        rf.has_settings = True
        rf.has_bank = False
        rf.has_tuning_step = False
        rf.can_odd_split = True
        rf.has_name = False
        rf.has_offset = True
        rf.has_mode = True
        rf.valid_modes = MODES
        rf.has_dtcs = True
        rf.has_rx_dtcs = True
        rf.has_dtcs_polarity = True
        rf.has_ctone = True
        rf.has_cross = True
        rf.valid_duplexes = ["", "-", "+", "split"]
        rf.valid_tmodes = ['', 'Tone', 'TSQL', 'DTCS', 'Cross']
        rf.valid_cross_modes = [
            "Tone->Tone",
            "DTCS->",
            "->DTCS",
            "Tone->DTCS",
            "DTCS->Tone",
            "->Tone",
            "DTCS->DTCS"]
        rf.valid_skips = SKIP_VALUES
        rf.valid_dtcs_codes = DTCS
        rf.memory_bounds = (1, self._upper)
        rf.valid_tuning_steps = [2.5, 5., 6.25, 10., 12.5, 25.]

        # normal dual bands
        rf.valid_bands = [self._vhf_range, self._uhf_range]

        return rf

    def process_mmap(self):
        """Process the mem map into the mem object"""

        # Get it
        self._memobj = bitwise.parse(MEM_FORMAT, self._mmap)

        # set the band limits as the memmap
        settings = self._memobj.settings
        self._vhf_range = _decode_ranges(settings.vhfl, settings.vhfh)
        self._uhf_range = _decode_ranges(settings.uhfl, settings.uhfh)

    def sync_in(self):
        """Download from radio"""
        data = _download(self)
        self._mmap = memmap.MemoryMapBytes(data)
        self.process_mmap()

    def sync_out(self):
        """Upload to radio"""

        try:
            _upload(self)
        except errors.RadioError:
            raise
        except Exception as e:
            raise errors.RadioError("Error: %s" % e)

    def _decode_tone(self, val, inv):
        """Parse the tone data to decode from mem, it returns:
        Mode (''|DTCS|Tone), Value (None|###), Polarity (None,N,R)"""

        if val == 0:
            return '', None, None
        elif val < 51:  # analog tone
            return 'Tone', TONES[val - 1], None
        elif val > 50:  # digital tone
            pol = "N"
            # polarity?
            if inv == 1:
                pol = "R"

            return 'DTCS', DTCS[val - 51], pol

    def _encode_tone(self, memtone, meminv, mode, tone, pol):
        """Parse the tone data to encode from UI to mem"""

        if mode == '' or mode is None:
            memtone.set_value(0)
            meminv.set_value(0)
        elif mode == 'Tone':
            # caching errors for analog tones.
            try:
                memtone.set_value(TONES.index(tone) + 1)
                meminv.set_value(0)
            except:
                msg = "TCSS Tone '%d' is not supported" % tone
                LOG.error(msg)
                raise errors.RadioError(msg)

        elif mode == 'DTCS':
            # caching errors for digital tones.
            try:
                memtone.set_value(DTCS.index(tone) + 51)
                if pol == "R":
                    meminv.set_value(True)
                else:
                    meminv.set_value(False)
            except:
                msg = "Digital Tone '%d' is not supported" % tone
                LOG.error(msg)
                raise errors.RadioError(msg)
        else:
            msg = "Internal error: invalid mode '%s'" % mode
            LOG.error(msg)
            raise errors.InvalidDataError(msg)

    def get_raw_memory(self, number):
        return repr(self._memobj.memory[number])

    def _get_special(self, number):
        if isinstance(number, str):
            return (getattr(self._memobj, number.lower()))
        elif number < 0:
            for k, v in SPECIALS.items():
                if number == v:
                    return (getattr(self._memobj, k.lower()))
        else:
            return self._memobj.channels[number-1]

    def get_memory(self, number):
        """Get the mem representation from the radio image"""
        _mem = self._get_special(number)

        # Create a high-level memory object to return to the UI
        mem = chirp_common.Memory()

        # Check if special or normal
        if isinstance(number, str):
            mem.number = SPECIALS[number]
            mem.extd_number = number
        else:
            mem.number = number

        if _mem.get_raw(asbytes=False)[0] == "\xFF":
            mem.empty = True
            return mem

        # Freq and offset
        mem.freq = int(_mem.rxfreq) * 10

        # TX freq (Stored as a difference)
        mem.offset = int(_mem.txoffset) * 10
        mem.duplex = ""

        # must work out the polarity
        if mem.offset != 0:
            if _mem.offminus == 1:
                mem.duplex = "-"
                #  tx below RX

            if _mem.offplus == 1:
                #  tx above RX
                mem.duplex = "+"

            # split RX/TX in different bands
            if mem.offset > 71000000:
                mem.duplex = "split"

                # show the actual value in the offset, depending on the shift
                if _mem.offminus == 1:
                    mem.offset = mem.freq - mem.offset
                if _mem.offplus == 1:
                    mem.offset = mem.freq + mem.offset

        # wide/narrow
        mem.mode = MODES[int(_mem.wide)]

        # skip
        mem.skip = SKIP_VALUES[_mem.noskip]

        # tone data
        rxtone = txtone = None
        txtone = self._decode_tone(_mem.txtone, _mem.ttondinv)
        rxtone = self._decode_tone(_mem.rxtone, _mem.rtondinv)
        chirp_common.split_tone_decode(mem, txtone, rxtone)

        return mem

    def set_memory(self, mem):
        """Set the memory data in the eeprom img from the UI"""
        # get the eprom representation of this channel
        _mem = self._get_special(mem.number)

        # if empty memory
        if mem.empty:
            # the channel itself
            _mem.set_raw("\xFF" * 16)
            # return it
            return mem

        # frequency
        _mem.rxfreq = mem.freq / 10

        # duplex/ offset Offset is an absolute value
        _mem.txoffset = mem.offset / 10

        # must work out the polarity
        if mem.duplex == "":
            _mem.offplus = 0
            _mem.offminus = 0
        elif mem.duplex == "+":
            _mem.offplus = 1
            _mem.offminus = 0
        elif mem.duplex == "-":
            _mem.offplus = 0
            _mem.offminus = 1
        elif mem.duplex == "split":
            if mem.freq > mem.offset:
                _mem.offplus = 0
                _mem.offminus = 1
                _mem.txoffset = (mem.freq - mem.offset) / 10
            else:
                _mem.offplus = 1
                _mem.offminus = 0
                _mem.txoffset = (mem.offset - mem.freq) / 10

        # wide/narrow
        _mem.wide = MODES.index(mem.mode)

        # skip
        _mem.noskip = SKIP_VALUES.index(mem.skip)

        # tone data
        ((txmode, txtone, txpol), (rxmode, rxtone, rxpol)) = \
            chirp_common.split_tone_encode(mem)
        self._encode_tone(_mem.txtone, _mem.ttondinv, txmode, txtone, txpol)
        self._encode_tone(_mem.rxtone, _mem.rtondinv, rxmode, rxtone, rxpol)

        return mem

    def get_settings(self):
        _settings = self._memobj.settings
        basic = RadioSettingGroup("basic", "Basic Settings")
        fm = RadioSettingGroup("fm", "FM Radio")
        adv = RadioSettingGroup("adv", "Advanced Settings")
        group = RadioSettings(basic, fm, adv)

        # ## Basic Settings
        rs = RadioSetting("tx_pwr", "TX Power",
                          RadioSettingValueList(
                            POWER_LIST, current_index=_settings.tx_pwr))
        basic.append(rs)

        rs = RadioSetting("channel", "Active Channel",
                          RadioSettingValueInteger(1, 20, _settings.channel))
        basic.append(rs)

        rs = RadioSetting("squelch", "Squelch Level",
                          RadioSettingValueInteger(0, 9, _settings.squelch))
        basic.append(rs)

        rs = RadioSetting("vox", "VOX Level",
                          RadioSettingValueInteger(0, 9, _settings.vox))
        basic.append(rs)

        # volume validation, as the OEM software set 0xFF on write
        _volume = _settings.volume
        if _volume > 7:
            _volume = 7
        rs = RadioSetting("volume", "Volume Level",
                          RadioSettingValueInteger(0, 7, _volume))
        basic.append(rs)

        rs = RadioSetting("scantype", "Scan Type",
                          RadioSettingValueList(SCAN_TYPE_LIST, current_index=_settings.scantype))
        basic.append(rs)

        rs = RadioSetting("timeout", "Time Out Timer (seconds)",
                          RadioSettingValueList(
                            TOT_LIST, current_index=_settings.timeout))
        basic.append(rs)

        rs = RadioSetting("voice", "Voice Prompt",
                          RadioSettingValueList(
                            LANGUAGE_LIST, current_index=_settings.voice))
        basic.append(rs)

        rs = RadioSetting("alarm", "Alarm Time",
                          RadioSettingValueList(
                            TIMER_LIST, current_index=_settings.alarm))
        basic.append(rs)

        rs = RadioSetting("backlight", "Backlight",
                          RadioSettingValueList(
                            BACKLIGHT_LIST,
                            current_index=_settings.backlight))
        basic.append(rs)

        rs = RadioSetting("blo", "Busy Lockout",
                          RadioSettingValueBoolean(_settings.blo))
        basic.append(rs)

        rs = RadioSetting("ste", "Squelch Tail Eliminate",
                          RadioSettingValueBoolean(_settings.ste))
        basic.append(rs)

        rs = RadioSetting("batsave", "Battery Save",
                          RadioSettingValueBoolean(_settings.batsave))
        basic.append(rs)

        rs = RadioSetting("lock", "Key Lock",
                          RadioSettingValueBoolean(_settings.lock))
        basic.append(rs)

        rs = RadioSetting("beep", "Key Beep",
                          RadioSettingValueBoolean(_settings.beep))
        basic.append(rs)

        # ## FM Settings
        rs = RadioSetting("fm_funct", "FM Function",
                          RadioSettingValueBoolean(_settings.fm_funct))
        fm.append(rs)

        rs = RadioSetting("fmrange", "FM Range",
                          RadioSettingValueList(
                            FM_RANGE_LIST, current_index=_settings.fmrange))
        fm.append(rs)

        # callbacks for the FM VFO
        def apply_fm_freq(setting, obj):
            value = int(setting.value.get_value() * 10) - 650
            LOG.debug("Setting fm_vfo = %s" % (value))
            if self._bw_shift:
                value = ((value & 0x00FF) << 8) | ((value & 0xFF00) >> 8)
            setattr(obj, setting.get_name(), value)

        # broadcast FM setting
        value = _settings.fm_vfo
        value_shifted = ((value & 0x00FF) << 8) | ((value & 0xFF00) >> 8)
        if value_shifted <= 108.0 * 10 - 650:
            # storage method 3 (discovered 2022)
            self._bw_shift = True
            _fm_vfo = value_shifted / 10.0 + 65
        elif value <= 108.0 * 10 - 650:
            # original storage method (2012)
            _fm_vfo = value / 10.0 + 65
        else:
            # unknown (undiscovered method or no FM chip?)
            _fm_vfo = False
        if _fm_vfo:
            rs = RadioSetting("fm_vfo", "FM Station",
                              RadioSettingValueFloat(65, 108, _fm_vfo))
            rs.set_apply_callback(apply_fm_freq, _settings)
            fm.append(rs)

        # ## Advanced
        def apply_limit(setting, obj):
            setattr(obj, setting.get_name(), int(setting.value) * 10)

        rs = RadioSetting("vhfl", "VHF Low Limit",
                          RadioSettingValueInteger(130, 174, int(
                              _settings.vhfl) / 10))
        rs.set_apply_callback(apply_limit, _settings)
        adv.append(rs)

        rs = RadioSetting("vhfh", "VHF High Limit",
                          RadioSettingValueInteger(130, 174, int(
                              _settings.vhfh) / 10))
        rs.set_apply_callback(apply_limit, _settings)
        adv.append(rs)

        rs = RadioSetting("uhfl", "UHF Low Limit",
                          RadioSettingValueInteger(400, 520, int(
                              _settings.uhfl) / 10))
        rs.set_apply_callback(apply_limit, _settings)
        adv.append(rs)

        rs = RadioSetting("uhfh", "UHF High Limit",
                          RadioSettingValueInteger(400, 520, int(
                              _settings.uhfh) / 10))
        rs.set_apply_callback(apply_limit, _settings)
        adv.append(rs)

        rs = RadioSetting("relaym", "Relay Mode",
                          RadioSettingValueList(RELAY_MODE_LIST,
                                                current_index=_settings.relaym))
        adv.append(rs)

        return group

    def set_settings(self, uisettings):
        _settings = self._memobj.settings

        for element in uisettings:
            if not isinstance(element, RadioSetting):
                self.set_settings(element)
                continue
            if not element.changed():
                continue

            try:
                name = element.get_name()
                value = element.value

                if element.has_apply_callback():
                    LOG.debug("Using apply callback")
                    element.run_apply_callback()
                else:
                    getattr(_settings, name)
                    setattr(_settings, name, value)

                LOG.debug("Setting %s: %s" % (name, value))
            except Exception:
                LOG.debug(element.get_name())
                raise

    @classmethod
    def match_model(cls, filedata, filename):
        match_size = False
        match_model = False

        # testing the file data size
        if len(filedata) == MEM_SIZE:
            match_size = True

            # DEBUG
            if debug is True:
                LOG.debug("BF-T1 matched!")

        # testing the firmware model fingerprint
        match_model = _model_match(cls, filedata)

        return match_size and match_model
