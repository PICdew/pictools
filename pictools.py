#!/usr/bin/env python3

"""Erase, read from and write to PIC flash memory. Uploads the PE to
SRAM using ICSP, which in turn accesses the flash memory.

                   +---------------+
 +------+          |               |          +---------+
 |      |   UART   |  Programmer   |   ICSP   |         |
 |  PC  o----------o               o----------o   PIC   |
 |      |          | (Arduino Due) |          |         |
 +------+          |               |          +---------+
                   +---------------+

"""

import os
import sys
import re
import argparse
import struct
import serial
import binascii
import bincopy
import subprocess
from tqdm import tqdm
import bitstruct


__version__ = '0.6.0'


ERRORS = {
    -22: "invalid argument",
    -34: "bad value, likely a memory address out of range",
    -71: "communication between programmer and PIC failed",
    -106: "PIC already connected",
    -107: "PIC is not connected",
    -110: "PIC command timeout",
    -1007: "invalid packet checksum",
    -1008: "flash write failed",
    -1009: "flash erase failed"
}

# Command types. Anything less than zero is error codes.
COMMAND_TYPE_FAILED = -1
COMMAND_TYPE_PING   =  1
COMMAND_TYPE_ERASE  =  2
COMMAND_TYPE_READ   =  3
COMMAND_TYPE_WRITE  =  4

PROGRAMMER_COMMAND_TYPE_PING          =  100
PROGRAMMER_COMMAND_TYPE_CONNECT       =  101
PROGRAMMER_COMMAND_TYPE_DISCONNECT    =  102
PROGRAMMER_COMMAND_TYPE_RESET         =  103
PROGRAMMER_COMMAND_TYPE_DEVICE_STATUS =  104
PROGRAMMER_COMMAND_TYPE_CHIP_ERASE    =  105

ERASE_TIMEOUT = 5
SERIAL_TIMEOUT = 1

READ_WRITE_CHUNK_SIZE = 1016

FLASH_ADDRESS               = 0x1d000000
FLASH_SIZE                  = 0x00040000
SFRS_ADDRESS                = 0x1f800000
SFRS_SIZE                   = 0x00010000
BOOT_FLASH_ADDRESS          = 0x1fc00000
BOOT_FLASH_SIZE             = 0x00001700
CONFIGURATION_BITS_ADDRESS  = 0x1fc01700
CONFIGURATION_BITS_SIZE     = 0x00000100
DEVICE_ID_ADDRESS           = 0x1f803660
UDID_ADDRESS                = 0x1fc41840

FLASH_RANGES = [
    (FLASH_ADDRESS, FLASH_SIZE),
    (BOOT_FLASH_ADDRESS, BOOT_FLASH_SIZE),
    (CONFIGURATION_BITS_ADDRESS, CONFIGURATION_BITS_SIZE),
]

RAMAPP_UPLOAD_INSTRUCTIONS_I_FMT = '''\
/**
 * This file was generated by pictools.py.
 */

/* Destination address in RAM to copy to. */
0xa00041a4,
0x00005084,

/* Upload the application. */
{},

/* Start the uploaded application. */
0xa00041b9,
0x00015339,
0x0f3c0019
'''


CONFIGURATION_FMT = '''\
FDEVOPT
  USERID: {}
  FVBUSIO: {}
  FUSBIDIO: {}
  ALTI2C: {}
  SOSCHP: {}
FICD
  ICS: {}
  JTAGEN: {}
FPOR
  LPBOREN: {}
  RETVR: {}
  BOREN: {}
FWDT
  FWDTEN: {}
  RCLKSEL: {}
  RWDTPS: {}
  WINDIS: {}
  FWDTWINSZ: {}
  SWDTPS: {}
FOSCSEL
  FCKSM: {}
  SOSCSEL: {}
  OSCIOFNC: {}
  POSCMOD: {}
  IESO: {}
  SOSCEN: {}
  PLLSRC: {}
  FNOSC: {}
FSEC
  CP: {}\
'''


DEVICE_ID_FMT = '''\
DEVID
  VER: {}
  DEVID: 0x{:08x}\
'''


UDID_FMT = '''\
UDID
  UDID1: 0x{:08x}
  UDID2: 0x{:08x}
  UDID3: 0x{:08x}
  UDID4: 0x{:08x}
  UDID5: 0x{:08x}\
'''


DEVICE_STATUS_FMT = '''\
STATUS: 0x{:02x}
  CPS:    {}
  NVMERR: {}
  CFGRDY: {}
  FCBUSY: {}
  DEVRST: {}\
'''


class CommandFailedError(Exception):

    def __init__(self, error):
        super().__init__()
        self.error = error

    def __str__(self):
        return format_error(self.error)


def crc_ccitt(data):
    """Calculate a CRC of given data.

    """

    msb = 0xff
    lsb = 0xff

    for c in bytearray(data):
        x = c ^ msb
        x ^= (x >> 4)
        msb = (lsb ^ (x >> 3) ^ (x << 4)) & 255
        lsb = (x ^ (x << 5)) & 255

    return (msb << 8) + lsb


def format_error(error):
    try:
        return 'error: ' + ERRORS[error]
    except KeyError:
        return 'Failed with {}.'.format(error)


def physical_flash_address(address):
    return address & 0x1fffffff


def serial_open(port):
    return serial.Serial(port,
                         baudrate=460800,
                         timeout=SERIAL_TIMEOUT)


def serial_open_ensure_connected_to_programmer(port):
    serial_connection = serial_open(port)

    programmer_ping(serial_connection)

    return serial_connection


def serial_open_ensure_connected(port):
    serial_connection = serial_open_ensure_connected_to_programmer(port)

    try:
        connect(serial_connection)
    except CommandFailedError as e:
        if e.error != -106:
            raise

    ping(serial_connection)

    return serial_connection


def serial_open_ensure_disconnected(port):
    serial_connection = serial_open_ensure_connected_to_programmer(port)

    try:
        disconnect(serial_connection)
    except CommandFailedError as e:
        if e.error != -107:
            raise

    return serial_connection


def packet_write(serial_connection, command_type, payload):
    """Write given packet to given serial connection.

    """

    header = struct.pack('>HH', command_type, len(payload))
    footer = struct.pack('>H', crc_ccitt(header + payload))

    serial_connection.write(header + payload + footer)


def packet_read(serial_connection):
    """Read a packet from given serial connection.

    """

    header = serial_connection.read(4)

    if len(header) != 4:
        print('error: failed to read packet header')
        return None, None

    command_type, payload_size = struct.unpack('>hH', header)

    if payload_size > 0:
        payload = serial_connection.read(payload_size)

        if len(payload) != payload_size:
            print('error: received {} bytes when expecting {}'.format(
                len(payload), payload_size))
            print('error: payload:', binascii.hexlify(payload))
            return None, None
    else:
        payload = b''

    footer = serial_connection.read(2)

    if len(footer) != 2:
        print('error: failed to read packet footer')
        return None, None

    crc = struct.unpack('>H', footer)[0]

    if crc != crc_ccitt(header + payload):
        print('error: crc mismatch of received packet')
        return None, None

    return command_type, payload


def execute_command(serial_connection, command_type, payload=None):
    """Execute given command and return the response payload.

    """

    if payload is None:
        payload = b''

    for _ in range(3):
        packet_write(serial_connection, command_type, payload)
        response_command_type, response_payload = packet_read(serial_connection)

        if response_command_type == command_type:
            return response_payload
        elif response_command_type == COMMAND_TYPE_FAILED:
            error = struct.unpack('>i', response_payload)[0]

            raise CommandFailedError(error)

    sys.exit('Communication failure.')


def read_to_file(serial_connection, ranges, outfile):
    binfile = bincopy.BinFile()

    for address, size in ranges:
        left = size

        print('Reading 0x{:08x}-0x{:08x}.'.format(address, address + size))

        with tqdm(total=left, unit=' bytes') as progress:
            while left > 0:
                if left > READ_WRITE_CHUNK_SIZE:
                    size = READ_WRITE_CHUNK_SIZE
                else:
                    size = left

                payload = struct.pack('>II', address, size)
                binfile.add_binary(execute_command(serial_connection,
                                                   COMMAND_TYPE_READ,
                                                   payload),
                                   address)
                address += size
                left -= size
                progress.update(size)

        print('Read complete.')

    with open(outfile, 'w') as fout:
        fout.write(binfile.as_srec())


def erase(serial_connection, address, size):
    """Erase flash memory.

    """

    payload = struct.pack('>II', address, size)

    print('Erasing 0x{:08x}-0x{:08x}.'.format(address, address + size))

    serial_connection.timeout = ERASE_TIMEOUT
    execute_command(serial_connection, COMMAND_TYPE_ERASE, payload)
    serial_connection.timeout = SERIAL_TIMEOUT

    print('Erase complete.')


def connect(serial_connection):
    execute_command(serial_connection, PROGRAMMER_COMMAND_TYPE_CONNECT)

    print('Connected to PIC.')


def disconnect(serial_connection):
    execute_command(serial_connection, PROGRAMMER_COMMAND_TYPE_DISCONNECT)

    print('Disconnected from PIC.')


def read_words(args, address, length):
    serial_connection = serial_open_ensure_connected(args.port)
    payload = struct.pack('>II', address, 4 * length)
    words = execute_command(serial_connection,
                            COMMAND_TYPE_READ,
                            payload)

    return bitstruct.byteswap(length * '4', words)


def ping(serial_connection):
    execute_command(serial_connection, COMMAND_TYPE_PING)

    print('PIC is alive.')


def programmer_ping(serial_connection):
    execute_command(serial_connection, PROGRAMMER_COMMAND_TYPE_PING)

    print('Programmer is alive.')


def do_reset(args):
    execute_command(serial_open_ensure_disconnected(args.port),
                    PROGRAMMER_COMMAND_TYPE_RESET)

    print('Resetted PIC.')


def do_device_status_print(args):
    status = execute_command(serial_open_ensure_connected_to_programmer(args.port),
                             PROGRAMMER_COMMAND_TYPE_DEVICE_STATUS)
    unpacked = bitstruct.unpack('u1p1u1p1u1u1p1u1', status)
    status = struct.unpack('B', status)[0]

    print(DEVICE_STATUS_FMT.format(status, *unpacked))


def do_flash_erase_chip(args):
    print('Erasing the chip.')

    execute_command(serial_open_ensure_disconnected(args.port),
                    PROGRAMMER_COMMAND_TYPE_CHIP_ERASE)

    print('Chip erase complete.')


def do_ping(args):
    # The open function pings the PIC.
    serial_open_ensure_connected(args.port)


def do_flash_erase(args):
    address = int(args.address, 0)
    size = int(args.size, 0)

    erase(serial_open_ensure_connected(args.port), address, size)


def do_flash_read(args):
    address = int(args.address, 0)
    size = int(args.size, 0)
    serial_connection = serial_open_ensure_connected(args.port)
    read_to_file(serial_connection, [(address, size)], args.outfile)


def do_flash_read_all(args):
    serial_connection = serial_open_ensure_connected(args.port)
    read_to_file(serial_connection, FLASH_RANGES, args.outfile)


def do_flash_write(args):
    serial_connection = serial_open_ensure_connected(args.port)

    f = bincopy.BinFile()
    f.add_file(args.binfile)

    erase_segments = []

    for address, data in f.segments:
        address = physical_flash_address(address)
        erase_segments.append((address, len(data)))

    if args.erase:
        for address, size in erase_segments:
            address = physical_flash_address(address)
            erase(serial_connection, address, size)

    print('Writing {} to flash.'.format(os.path.abspath(args.binfile)))

    for address, data in f.segments:
        address = physical_flash_address(address)

        print('Writing 0x{:08x}-0x{:08x}.'.format(address,
                                                  address + len(data)))

        with tqdm(total=len(data), unit=' bytes') as progress:
            left = len(data)

            while left > 0:
                if left > READ_WRITE_CHUNK_SIZE:
                    size = READ_WRITE_CHUNK_SIZE
                else:
                    size = left

                payload = struct.pack('>II', address, size)
                payload += data[:size]
                data = data[size:]
                execute_command(serial_connection, COMMAND_TYPE_WRITE, payload)
                address += size
                left -= size
                progress.update(size)

        print('Write complete.')

    if args.verify:
        print('Verifying written data.')

        for address, data in f.segments:
            address = physical_flash_address(address)

            print('Verifying 0x{:08x}-0x{:08x}.'.format(address,
                                                        address + len(data)))

            with tqdm(total=len(data), unit=' bytes') as progress:
                left = len(data)

                while left > 0:
                    if left > READ_WRITE_CHUNK_SIZE:
                        size = READ_WRITE_CHUNK_SIZE
                    else:
                        size = left

                    payload = struct.pack('>II', address, size)
                    read_data = execute_command(serial_connection,
                                                COMMAND_TYPE_READ,
                                                payload)

                    if bytearray(read_data) != data[:size]:
                        sys.exit(
                            'Verify failed at address 0x{:x}.'.format(address))

                    address += size
                    left -= size
                    data = data[size:]
                    progress.update(size)

            print('Verify complete.')


def do_configuration_print(args):
    config = read_words(args, CONFIGURATION_BITS_ADDRESS + 0xc0, 10)
    unpacked = bitstruct.unpack('p32'                          # RESERVED
                                'u16u1u1p9u1u1p3'              # FDEVOPT
                                'p27u2u1p2'                    # FICD
                                'p28u1u1u2'                    # FPOR
                                'p16u1u2u5u1u2u5'              # FWDT
                                'p16u2p1u1p1u1u2u1u1p1u1p1u3'  # FOSCSEL
                                'u1p31'                        # FSEC
                                'p32'                          # RESERVED
                                'p32'                          # RESERVED
                                'p32',                         # RESERVED
                                config)

    print(CONFIGURATION_FMT.format(*unpacked))


def do_device_id_print(args):
    device_id = read_words(args, DEVICE_ID_ADDRESS, 1)
    unpacked = bitstruct.unpack('u4u28', device_id)

    print(DEVICE_ID_FMT.format(*unpacked))


def do_udid_print(args):
    udid = read_words(args, UDID_ADDRESS, 5)
    unpacked = bitstruct.unpack(5 * 'u32', udid)

    print(UDID_FMT.format(*unpacked))


def do_programmer_ping(args):
    serial_connection = serial_open(args.port)

    programmer_ping(serial_connection)


def do_generate_ramapp_upload_instructions(args):
    instructions = []

    disassembly = subprocess.check_output([
        'mips-unknown-elf-objdump', '-d', args.elffile
    ]).decode('utf-8')

    instructions = []

    for line in disassembly.splitlines():
        mo = re.match(r'([a-f0-9]+):\t([^\t]+)', line)

        if mo:
            address = int(mo.group(1), 16)
            data = mo.group(2).replace(' ', '')
            size = len(data) // 2

            if len(data) == 8:
                data = data[4:] + data[:4]

            if len(instructions) > 0:
                prev = instructions[-1]
                prev_end = prev[0] + prev[1]
                padding_size = address - prev_end

                for i in range(padding_size // 2):
                    instructions.append((prev_end + 2 * i, 2, '0000'))

            instructions.append((address, size, data))

    pairs = []
    leftover = None

    for address, size, data in instructions:
        if size == 4:
            if leftover:
                pairs.append((data[4:], leftover))
                leftover = data[:4]
            else:
                pairs.append((data[:4], data[4:]))
        else:
            if leftover:
                pairs.append((data, leftover))
                leftover = None
            else:
                leftover = data

    def hex8(value):
        return '0x{}'.format(value)

    instructions = []

    for high, low in pairs:
        instructions.append(hex8(high + '41a6'))
        instructions.append(hex8(low + '50c6'))
        instructions.append(hex8('0000f8c4'))
        instructions.append(hex8('00043084'))

    with open(args.outfile, "w") as fout:
        fout.write(RAMAPP_UPLOAD_INSTRUCTIONS_I_FMT.format(
            ',\n'.join(instructions)))


def _main():
    description = (
        "Erase, read from and write to PIC flash memory, and more. Uploads "
        "the RAM application to the PIC RAM over ICSP, which in turn accesses "
        "the flash memory.")
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-p', '--port',
                        default='/dev/ttyUSB1',
                        help='Programmer serial port (default: /dev/ttyUSB1).')
    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('--version',
                        action='version',
                        version=__version__,
                        help='Print version information and exit.')

    # Python 3 workaround for help output if subcommand is missing.
    subparsers = parser.add_subparsers(dest='one of the above')
    subparsers.required = True

    subparser = subparsers.add_parser('reset',
                                      help='Reset the PIC.')
    subparser.set_defaults(func=do_reset)

    subparser = subparsers.add_parser(
        'ping',
        help='Test if the PIC is alive and executing the RAM application.')
    subparser.set_defaults(func=do_ping)

    subparser = subparsers.add_parser('flash_erase',
                                      help='Erase given flash range.')
    subparser.add_argument('address')
    subparser.add_argument('size')
    subparser.set_defaults(func=do_flash_erase)

    subparser = subparsers.add_parser('flash_read',
                                      help='Read from the flash memory.')
    subparser.add_argument('address')
    subparser.add_argument('size')
    subparser.add_argument('outfile')
    subparser.set_defaults(func=do_flash_read)

    subparser = subparsers.add_parser(
        'flash_read_all',
        help='Read program flash, boot flash and configuration memory.')
    subparser.add_argument('outfile')
    subparser.set_defaults(func=do_flash_read_all)

    subparser = subparsers.add_parser(
        'flash_write',
        help=('Write given file to flash. Optionally performs erase and '
              'verify operations.'))
    subparser.add_argument('-e', '--erase', action='store_true')
    subparser.add_argument('-v', '--verify', action='store_true')
    subparser.add_argument('binfile')
    subparser.set_defaults(func=do_flash_write)

    subparser = subparsers.add_parser(
        'flash_erase_chip',
        help='Erases program flash, boot flash and configuration memory.')
    subparser.set_defaults(func=do_flash_erase_chip)

    subparser = subparsers.add_parser('configuration_print',
                                      help='Print the configuration memory.')
    subparser.set_defaults(func=do_configuration_print)

    subparser = subparsers.add_parser('device_id_print',
                                      help='Print the device id.')
    subparser.set_defaults(func=do_device_id_print)

    subparser = subparsers.add_parser('udid_print',
                                      help='Print the unique chip id.')
    subparser.set_defaults(func=do_udid_print)

    subparser = subparsers.add_parser('device_status_print',
                                      help='Print the device status.')
    subparser.set_defaults(func=do_device_status_print)

    subparser = subparsers.add_parser(
        'programmer_ping',
        help='Test if the programmer is alive.')
    subparser.set_defaults(func=do_programmer_ping)

    subparser = subparsers.add_parser(
        'generate_ramapp_upload_instructions',
        help='Generate the RAM application C source file.')
    subparser.add_argument('elffile')
    subparser.add_argument('outfile')
    subparser.set_defaults(func=do_generate_ramapp_upload_instructions)

    args = parser.parse_args()

    if args.debug:
        args.func(args)
    else:
        try:
            args.func(args)
        except BaseException as e:
            sys.exit(str(e))


if __name__ == "__main__":
    _main()
