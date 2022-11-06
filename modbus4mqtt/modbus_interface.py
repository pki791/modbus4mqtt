from time import time, sleep
from enum import Enum
import logging
from queue import Queue
from urllib.parse import urlparse, parse_qs
from pymodbus import exceptions

DEFAULT_SCAN_BATCHING = 100
MIN_SCAN_BATCHING = 1
MAX_SCAN_BATCHING = 100
DEFAULT_WRITE_BLOCK_INTERVAL_S = 0.2
DEFAULT_WRITE_SLEEP_S = 0.05
DEFAULT_READ_SLEEP_S = 0.05

class WordOrder(Enum):
    HighLow = 1
    LowHigh = 2

class DeviceUnit():
  def __init__(self, unit, table):
    self.unit = unit
    self.table = table
  
  @classmethod
  def get_unit(cls, instance, unit):
    if isinstance(instance, DeviceUnit):
      return instance.unit
    return unit
  
  @classmethod
  def get_table(cls, instance):
    if isinstance(instance, DeviceUnit):
      return instance.table
    if isinstance(instance, str):
      return instance
    return instance[1]
    
class modbus_interface():

    def __init__(self, url, scan_batching=None, word_order=WordOrder.HighLow, options={}):
        self._url = urlparse(url)
        # This is a dict of sets. Each key represents one table of modbus registers.
        # At the moment it has 'input' and 'holding'
        self._tables = {'input': set(), 'holding': set()}

        # This is a dicts of dicts. These hold the current values of the interesting registers
        self._values = {'input': {}, 'holding': {}}

        self._planned_writes = Queue()
        self._writing = False
        if options.get('word_order', 'highlow').lower() == 'lowhigh':
            self._word_order = WordOrder.LowHigh
        else:
            self._word_order = WordOrder.HighLow

        self._unit = options.get('unit', 0x01)
        self._range_batching = options.get('range_batching', False)
        scan_batching = options.get('scan_batching', None)
        if scan_batching is None:
            self._scan_batching = DEFAULT_SCAN_BATCHING
        else:
            if scan_batching < MIN_SCAN_BATCHING:
                logging.warning("Bad value for scan_batching: {}. Enforcing minimum value of {}".format(scan_batching, MIN_SCAN_BATCHING))
                self._scan_batching = MIN_SCAN_BATCHING
            elif scan_batching > MAX_SCAN_BATCHING:
                logging.warning("Bad value for scan_batching: {}. Enforcing maximum value of {}".format(scan_batching, MAX_SCAN_BATCHING))
                self._scan_batching = MAX_SCAN_BATCHING
            else:
                self._scan_batching = scan_batching

    def connect(self):
        # Connects to the modbus device
        if self._url.scheme == 'serial':
            from pymodbus.client.sync import ModbusSerialClient, ModbusRtuFramer
            port     = self._url.path or self._url.netloc
            params   = parse_qs(self._url.query)
            baudrate = params.get('baud', 9600)
            comset   = params.get('comset', '')
            if comset:
              (bytesize, parity, stopbits) = comset[0:2]
            else:
              (bytesize, parity, stopbits) = (
                params.get('bytesize', 8),
                params.get('parity', 'N'),
                params.get('stopbits', 1)
              )
            # baudrate parity stopbits
            self._mb = ModbusSerialClient(port=port,
                                       method='rtu',
                                       timeout=1,RetryOnEmpty=True, retries=1
                                       , baudrate=baudrate, parity=parity, stopbits=stopbits)
        else:
            host=self._url.hostname
            port=self._url.port if self._url.hasattr('port') else 502
            if self._url.scheme == 'sungrow':
                from SungrowModbusTcpClient import SungrowModbusTcpClient
                # Some later versions of the sungrow inverter firmware encrypts the payloads of
                # the modbus traffic. https://github.com/rpvelloso/Sungrow-Modbus is a drop-in
                # replacement for ModbusTcpClient that manages decrypting the traffic for us.
                self._mb = SungrowModbusTcpClient.SungrowModbusTcpClient(host=host, port=port,
                                                  framer=ModbusSocketFramer, timeout=1,
                                                  RetryOnEmpty=True, retries=1)
            else:
                from pymodbus.client.sync import ModbusTcpClient, ModbusSocketFramer
                self._mb = ModbusTcpClient(host, port,
                                           framer=ModbusSocketFramer, timeout=1,
                                           RetryOnEmpty=True, retries=1)

    def add_monitor_register(self, table, addr, type='uint16'):
        # Accepts a modbus register and table to monitor
        if table not in self._tables:
            raise ValueError("Unsupported table type. Please only use: {}".format(self._tables.keys()))
        # Register enough sequential addresses to fill the size of the register type.
        # Note: Each address provides 2 bytes of data.
        for i in range(type_length(type)):
            self._tables[table].add(addr+i)

    def prepare(self):
      self._registers = dict()
      for (table, registers) in self._tables.items():
        self._registers[table] = sorted(registers)
    
    def _get_scan_ranges(self, registers):
      ranges = [ ]
      if self._range_batching:
        offset = -1
        count = 0
        for k in registers:
          if count < self._scan_batching:
            if offset + count == k:
              count = count + 1
              continue
          if count > 0:
            ranges.append( ( offset, count ) )
          offset = k
          count = 1
      else:
        offset = registers[0]
        count = self._scan_batching
        while offset+count <= registers[-1]:
          ranges.append( ( offset, count ) )
          offset = offset + count
        count = registers[-1] - offset + 1

      if count > 0:
        ranges.append( ( offset, count ) )

      return ranges

    
    def poll(self):
        # Polls for the values marked as interesting in self._registers.
        for (table, registers) in self._registers.items():
            ranges = self._get_scan_ranges(registers)
            
            for ( group, count ) in ranges:
              try:
                  values = self._scan_value_range(table, group, count)
                  for x in range(0, count):
                      key = group + x
                      self._values[table][key] = values[x]
                  # Avoid back-to-back read operations that could overwhelm some modbus devices.
                  sleep(DEFAULT_READ_SLEEP_S)
              except ValueError as e:
                  logging.exception("{}".format(e))
        self._process_writes()

    def get_value(self, table, addr, type='uint16'):
        if table not in self._values:
            raise ValueError("Unsupported table type. Please only use: {}".format(self._values.keys()))
        if addr not in self._values[table]:
            raise ValueError("Unpolled address. Use add_monitor_register(addr, table) to add a register to the polled list.")
        # Read sequential addresses to get enough bytes to satisfy the type of this register.
        # Note: Each address provides 2 bytes of data.
        value = bytes(0)
        type_len = type_length(type)
        for i in range(type_len):
            if self._word_order == WordOrder.HighLow:
                data = self._values[table][addr + i]
            else:
                data = self._values[table][addr + (type_len-i-1)]
            value += data.to_bytes(2,'big')
        value = _convert_from_bytes_to_type(value, type)
        return value

    def set_value(self, table, addr, value, mask=0xFFFF, type='uint16'):
        if DeviceUnit.get_table(table) != 'holding':
            # I'm not sure if this is true for all devices. I might support writing to coils later,
            # so leave this door open.
            raise ValueError("Can only set values in the holding table.")

        bytes_to_write = _convert_from_type_to_bytes(value, type)
        # Put the bytes into _planned_writes stitched into two-byte pairs

        type_len = type_length(type)
        for i in range(type_len):
            if self._word_order == WordOrder.HighLow:
                value = _convert_from_bytes_to_type(bytes_to_write[i*2:i*2+2], 'uint16')
            else:
                value = _convert_from_bytes_to_type(bytes_to_write[(type_len-i-1)*2:(type_len-i-1)*2+2], 'uint16')
            self._planned_writes.put((table, addr+i, value, mask))

        self._process_writes()

    def _process_writes(self, max_block_s=DEFAULT_WRITE_BLOCK_INTERVAL_S):
        # TODO I am not entirely happy with this system. It's supposed to prevent
        # anything overwhelming the modbus interface with a heap of rapid writes,
        # but without its own event loop it could be quite a while between calls to
        # .poll()...
        if self._writing:
            return
        write_start_time = time()
        try:
            self._writing = True
            while not self._planned_writes.empty() and (time() - write_start_time) < max_block_s:
                device_unit, addr, value, mask = self._planned_writes.get()
                unit = DeviceUnit.get_unit(device_unit, self._unit)
                if mask == 0xFFFF:
                    self._mb.write_register(addr, value, unit=unit)
                else:
                    # https://pymodbus.readthedocs.io/en/latest/source/library/pymodbus.client.html?highlight=mask_write_register#pymodbus.client.common.ModbusClientMixin.mask_write_register
                    # https://www.mathworks.com/help/instrument/modify-the-contents-of-a-holding-register-using-a-mask-write.html
                    # Result = (register value AND andMask) OR (orMask AND (NOT andMask))
                    # This bit-shift weirdness is to avoid a mask of 0x0001 resulting in a ~mask of -2, which pymodbus doesn't like.
                    # This means the result will be 65534, AKA 0xFFFE.
                    # This specific read-before-write operation doesn't work on my modbus solar inverter -
                    # I get "Modbus Error: [Input/Output] Modbus Error: [Invalid Message] Incomplete message received, expected at least 8 bytes (0 received)"
                    # I suspect it's a different modbus opcode that tries to do clever things that my device doesn't support.
                    # result = self._mb.mask_write_register(address=addr, and_mask=(1<<16)-1-mask, or_mask=value, unit=0x01)
                    # print("Result: {}".format(result))
                    old_value = self._scan_value_range(device_unit, addr, 1)[0]
                    and_mask = (1<<16)-1-mask
                    or_mask = value
                    new_value = (old_value & and_mask) | (or_mask & (mask))
                    self._mb.write_register(addr, new_value, unit=unit)
                sleep(DEFAULT_WRITE_SLEEP_S)
        except Exception as e:
            # BUG catch only the specific exception that means pymodbus failed to write to a register
            # the modbus device doesn't support, not an error at the TCP layer.
            logging.exception("Failed to write to modbus device: {}".format(e))
        finally:
            self._writing = False

    def _scan_value_range(self, device_unit, start, count):
        result = None
        table = DeviceUnit.get_table(device_unit)
        unit = DeviceUnit.get_unit(device_unit, self._unit)
        if table == 'input':
            result = self._mb.read_input_registers(start, count, unit=unit)
        elif table == 'holding':
            result = self._mb.read_holding_registers(start, count, unit=unit)
        try:
            return result.registers
        except:
            # The result doesn't have a registers attribute, something has gone wrong!
            raise ValueError("Failed to read {} {} table registers starting from {}: {}".format(count, table, start, result))

def type_length(type):
    # Return the number of addresses needed for the type.
    # Note: Each address provides 2 bytes of data.
    if type in ['int16', 'uint16']:
        return 1
    elif type in ['int32', 'uint32']:
        return 2
    elif type in ['int64', 'uint64']:
        return 4
    raise ValueError ("Unsupported type {}".format(type))

def type_signed(type):
    # Returns whether the provided type is signed
    if type in ['uint16', 'uint32', 'uint64']:
        return False
    elif type in ['int16', 'int32', 'int64']:
        return True
    raise ValueError ("Unsupported type {}".format(type))

def _convert_from_bytes_to_type(value, type):
    type = type.strip().lower()
    signed = type_signed(type)
    return int.from_bytes(value,byteorder='big',signed=signed)

def _convert_from_type_to_bytes(value, type):
    type = type.strip().lower()
    signed = type_signed(type)
    # This can throw an OverflowError in various conditons. This will usually
    # percolate upwards and spit out an exception from on_message.
    return int(value).to_bytes(type_length(type)*2,byteorder='big',signed=signed)
