from pathlib import Path
import functools
from typing import Tuple, Set, Callable, Optional, Dict, NamedTuple, Iterator, Sequence
from collections import OrderedDict
from enum import Enum
import weakref
import logging
import warnings
import time
from itertools import chain

try:
    import zhinst.ziPython
    import zhinst.utils
except ImportError:
    warnings.warn('Zurich Instruments LabOne python API is distributed via the Python Package Index. Install with pip.')
    raise

import numpy as np
import textwrap
import time

from qupulse.utils.types import ChannelID, TimeType, time_from_fraction
from qupulse._program._loop import Loop, make_compatible
from qupulse._program.waveforms import Waveform
from qupulse.hardware.awgs.base import AWG, ChannelNotFoundException
from qupulse.hardware.util import get_sample_times

class HDAWGChannelGrouping(Enum):
    """How many independent sequencers should run on the AWG and how the outputs should be grouped by sequencer."""
    CHAN_GROUP_4x2 = 0  # 4x2 with HDAWG8; 2x2 with HDAWG4.  /dev.../awgs/0..3/
    CHAN_GROUP_2x4 = 1  # 2x4 with HDAWG8; 1x4 with HDAWG4.  /dev.../awgs/0 & 2/
    CHAN_GROUP_1x8 = 2  # 1x8 with HDAWG8.                   /dev.../awgs/0/


class HDAWGVoltageRange(Enum):
    """All available voltage ranges for the HDAWG wave outputs. Define maximum output voltage."""
    RNG_5V = 5
    RNG_4V = 4
    RNG_3V = 3
    RNG_2V = 2
    RNG_1V = 1
    RNG_800mV = 0.8
    RNG_600mV = 0.6
    RNG_400mV = 0.4
    RNG_200mV = 0.2
    
def valid_channel(function_object):
    """Check if channel is a valid AWG channels. Expects channel to be 2nd argument after self."""
    @functools.wraps(function_object)
    def valid_fn(*args, **kwargs):
        if len(args) < 2:
            raise HDAWGTypeError('Channel is an required argument.')
        channel = args[1]  # Expect channel to be second positional argument after self.
        if channel not in range(1, 9):
            raise ChannelNotFoundException(channel)
        value = function_object(*args, **kwargs)
        return value
    return valid_fn


class HDAWGRepresentation:
    """HDAWGRepresentation represents an HDAWG8 instruments and manages a LabOne data server api session. A data server
    must be running and the device be discoverable. Channels are per default grouped into pairs."""

    __version__ = 0.1
    
    def __init__(self, device_serial: str = None,
                 device_interface: str = '1GbE',
                 data_server_addr: str = 'localhost',
                 data_server_port: int = 8004,
                 api_level_number: int = 6,
                 reset: bool = False,
                 timeout: float = 120,
                 channel_grouping = HDAWGChannelGrouping.CHAN_GROUP_4x2,
                 sample_rate_index = 0) -> None:
        """
        :param device_serial:     Device serial that uniquely identifies this device to the LabOne data server
        :param device_interface:  Either '1GbE' for ethernet or 'USB'
        :param data_server_addr:  Data server address. Must be already running. Default: localhost
        :param data_server_port:  Data server port. Default: 8004 for HDAWG, MF and UHF devices
        :param api_level_number:  Version of API to use for the session, higher number, newer. Default: 6 most recent
        :param reset:             Reset device before initialization
        :param timeout:           Timeout in seconds for uploading
        """
        self._api_session = zhinst.ziPython.ziDAQServer(data_server_addr, data_server_port, api_level_number)
        zhinst.utils.api_server_version_check(self.api_session)  # Check equal data server and api version.
        self.api_session.connectDevice(device_serial, device_interface)
        self._dev_ser = device_serial
        self.channel_grouping = channel_grouping
        self.sample_rate_index = sample_rate_index

        if reset:
            # Create a base configuration: Disable all available outputs, awgs, demods, scopes,...
            zhinst.utils.disable_everything(self.api_session, self.serial)


        if channel_grouping == HDAWGChannelGrouping.CHAN_GROUP_4x2:
            self._channel_groups = (
                HDAWGChannelGroup(self, (1, 2), str(self.serial) + '_AB', timeout),
                HDAWGChannelGroup(self, (3, 4), str(self.serial) + '_CD', timeout),
                HDAWGChannelGroup(self, (5, 6), str(self.serial) + '_EF', timeout),
                HDAWGChannelGroup(self, (7, 8), str(self.serial) + '_GH', timeout),
            )
        elif channel_grouping == HDAWGChannelGrouping.CHAN_GROUP_2x4:
            self._channel_groups = (
                HDAWGChannelGroup(self, (1, 2, 3, 4), str(self.serial) + '_ABCD', timeout),
                HDAWGChannelGroup(self, (5, 6, 7, 8), str(self.serial) + '_EFGH', timeout),
            )
        elif channel_grouping == HDAWGChannelGrouping.CHAN_GROUP_1x8:
            self._channel_groups = (
                HDAWGChannelGroup(self, range(1, 9), str(self.serial) + '_full', timeout),
            )
        else:
            # FIXME: raise exception
            self._channel_groups = ()

        self._initialize()

    @property
    def number_of_channel_groups(self) -> int:
        return len(self._channel_groups)

    def channel_group(self, idx: int) -> 'HDAWGChannelGroup':
        return self._channel_groups[idx]

    def existing_programs(self):
        
        names = [channel_group._program_manager._known_programs.keys() for channel_group in self._channel_groups ]
        return list(chain(*names))

    @property
    def channel_pair_AB(self) -> 'HDAWGChannelGroup':
        # FIXME raise exception if incompatible channel grouping
        return self._channel_groups[0]

    @property
    def channel_pair_CD(self) -> 'HDAWGChannelGroup':
        # FIXME raise exception if incompatible channel grouping
        return self._channel_groups[1]

    @property
    def channel_pair_EF(self) -> 'HDAWGChannelGroup':
        # FIXME raise exception if incompatible channel grouping
        return self._channel_groups[2]

    @property
    def channel_pair_GH(self) -> 'HDAWGChannelGroup':
        # FIXME raise exception if incompatible channel grouping
        return self._channel_groupd[3]

    @property
    def api_session(self) -> zhinst.ziPython.ziDAQServer:
        return self._api_session

    @property
    def serial(self) -> str:
        return self._dev_ser

    def _initialize(self) -> None:
        settings = []
        settings.append(['/{}/system/awg/channelgrouping'.format(self.serial), self.channel_grouping.value])
        settings.append(['/{}/awgs/*/time'.format(self.serial), self.sample_rate_index])
        settings.append(['/{}/sigouts/*/range'.format(self.serial), HDAWGVoltageRange.RNG_1V.value])
        settings.append(['/{}/awgs/*/outputs/*/amplitude'.format(self.serial), 3.0])  # Default amplitude factor 1.0
        settings.append(['/{}/awgs/*/outputs/*/modulation/mode'.format(self.serial), HDAWGModulationMode.OFF.value])
        settings.append(['/{}/awgs/*/userregs/*'.format(self.serial), 0])  # Reset all user registers to 0.
        settings.append(['/{}/awgs/*/single'.format(self.serial), 1])  # Single execution mode of sequence.
        for ch in range(0, 8):  # Route marker 1 signal for each channel to marker output.
            if ch % 2 == 0:
                output = HDAWGTriggerOutSource.OUT_1_MARK_1.value
            else:
                output = HDAWGTriggerOutSource.OUT_2_MARK_1.value
            settings.append(['/{}/triggers/out/{}/source'.format(self.serial, ch), output])

        self.api_session.set(settings)
        self.api_session.sync()  # Global sync: Ensure settings have taken effect on the device.

    def reset(self) -> None:
        zhinst.utils.disable_everything(self.api_session, self.serial)
        self._initialize()
        for cg in self._channel_groups:
            cg.clear()

    @valid_channel
    def offset(self, channel: int, voltage: float = None) -> float:
        """Query channel offset voltage and optionally set it."""
        node_path = '/{}/sigouts/{:d}/offset'.format(self.serial, channel-1)
        if voltage is not None:
            self.api_session.setDouble(node_path, voltage)
            self.api_session.sync()  # Global sync: Ensure settings have taken effect on the device.
        return self.api_session.getDouble(node_path)

    @valid_channel
    def range(self, channel: int, voltage: float = None) -> float:
        """Query channel voltage range and optionally set it. The instruments selects the next higher available range.
        This is the one-sided range Vp. Total range: -Vp...Vp"""
        node_path = '/{}/sigouts/{:d}/range'.format(self.serial, channel-1)
        if voltage is not None:
            self.api_session.setDouble(node_path, voltage)
            self.api_session.sync()  # Global sync: Ensure settings have taken effect on the device.
        return self.api_session.getDouble(node_path)

    @valid_channel
    def output(self, channel: int, status: bool = None) -> bool:
        """Query channel signal output status (enabled/disabled) and optionally set it. Corresponds to front LED."""
        node_path = '/{}/sigouts/{:d}/on'.format(self.serial, channel-1)
        if status is not None:
            self.api_session.setInt(node_path, int(status))
            self.api_session.sync()  # Global sync: Ensure settings have taken effect on the device.
        return bool(self.api_session.getInt(node_path))

    def get_status_table(self):
        """Return node tree of instrument with all important settings, as well as each channel group as tuple."""
        session_data = (self.api_session.get('/{}/*'.format(self.serial)), )
        return session_data + (cg.awg_module.get('awgModule/*') for cg in self._channel_groups)


class HDAWGTriggerOutSource(Enum):
    """Assign a signal to a marker output. This is per AWG Core."""
    AWG_TRIG_1 = 0  # Trigger output assigned to AWG trigger 1, controlled by AWG sequencer commands.
    AWG_TRIG_2 = 1  # Trigger output assigned to AWG trigger 2, controlled by AWG sequencer commands.
    AWG_TRIG_3 = 2  # Trigger output assigned to AWG trigger 3, controlled by AWG sequencer commands.
    AWG_TRIG_4 = 3  # Trigger output assigned to AWG trigger 4, controlled by AWG sequencer commands.
    OUT_1_MARK_1 = 4  # Trigger output assigned to output 1 marker 1.
    OUT_1_MARK_2 = 5  # Trigger output assigned to output 1 marker 2.
    OUT_2_MARK_1 = 6  # Trigger output assigned to output 2 marker 1.
    OUT_2_MARK_2 = 7  # Trigger output assigned to output 2 marker 2.
    TRIG_IN_1 = 8  # Trigger output assigned to trigger inout 1.
    TRIG_IN_2 = 9  # Trigger output assigned to trigger inout 2.
    TRIG_IN_3 = 10  # Trigger output assigned to trigger inout 3.
    TRIG_IN_4 = 11  # Trigger output assigned to trigger inout 4.
    TRIG_IN_5 = 12  # Trigger output assigned to trigger inout 5.
    TRIG_IN_6 = 13  # Trigger output assigned to trigger inout 6.
    TRIG_IN_7 = 14  # Trigger output assigned to trigger inout 7.
    TRIG_IN_8 = 15  # Trigger output assigned to trigger inout 8.
    HIGH = 17 # Trigger output is set to high.
    LOW = 18 # Trigger output is set to low.


class HDAWGModulationMode(Enum):
    """Modulation mode of waveform generator."""
    OFF = 0  # AWG output goes directly to signal output.
    SINE_1 = 1  # AWG output multiplied with sine generator signal 0.
    SINE_2 = 2  # AWG output multiplied with sine generator signal 1.
    FG_1 = 3  # AWG output multiplied with function generator signal 0. Requires FG option.
    FG_2 = 4  # AWG output multiplied with function generator signal 1. Requires FG option.
    ADVANCED = 5  # AWG output modulates corresponding sines from modulation carriers.


class HDAWGRegisterFunc(Enum):
    """Functions of registers for sequence control."""
    PROG_SEL = 1  # Use this register to select which program in the sequence should be running.
    PROG_IDLE = 0  # This value of the PROG_SEL register is reserved for the idle waveform.


class HDAWGChannelGroup(AWG):
    """Represents a group of channels of the Zurich Instruments HDAWG as an independent AWG entity.
    Depending on the chosen HDAWG grouping, a group can have 2, 4 or 8 channels.
    It represents a set of channels that have to have(hardware enforced) the same:
        -control flow
        -sample rate

    It keeps track of the AWG state and manages waveforms and programs on the hardware.
    """

    def __init__(self, hdawg_device: HDAWGRepresentation,
                 channels: Sequence[int],
                 identifier: str,
                 timeout: float) -> None:
        super().__init__(identifier)
        self._device = weakref.proxy(hdawg_device)

        if hdawg_device.channel_grouping == HDAWGChannelGrouping.CHAN_GROUP_4x2:
            if channels not in ((1, 2), (3, 4), (5, 6), (7, 8)):
                raise HDAWGValueError('Invalid 4x2 channel pair: {}'.format(channels))
        elif hdawg_device.channel_grouping == HDAWGChannelGrouping.CHAN_GROUP_1x8:
            if channels != range(1, 9):
                raise HDAWGValueError('Invalid 1x8 channel list: {}'.format(channels))
        elif hdawg_device.channel_grouping == HDAWGChannelGrouping.CHAN_GROUP_2x4:
            raise HDAWGValueError('Channel grouping 2x4 not yet supported')

        self._channels = channels
        self._timeout = timeout

        self._awg_module = self.device.api_session.awgModule()
        self.awg_module.set('awgModule/device', self.device.serial)
        self.awg_module.set('awgModule/index', self.awg_group_index)
        self.awg_module.execute()
        # Seems creating AWG module sets SINGLE (single execution mode of sequence) to 0 per default.
        self.device.api_session.setInt('/{}/awgs/{:d}/single'.format(self.device.serial, self.awg_group_index), 1)

        self._wave_manager = HDAWGWaveManager(self.user_directory, self.identifier)
        self._program_manager = HDAWGProgramManager(self._wave_manager, number_of_channels=len(channels))
        self._current_program = None  # Currently armed program.

    @property
    def num_channels(self) -> int:
        """Number of channels"""
        return len(self._channels)

    @property
    def num_markers(self) -> int:
        """Number of marker channels"""
        return len(self._channels)  # Actually 2*len(self._channels) available.

    def upload(self, name: str,
               program: Loop,
               channels: Tuple[Optional[ChannelID], Optional[ChannelID]],
               markers: Tuple[Optional[ChannelID], Optional[ChannelID]],
               voltage_transformation: Tuple[Callable],
               force: bool = False) -> None:
        """Upload a program to the AWG.

        Physically uploads all waveforms required by the program - excluding those already present -
        to the device and sets up playback sequences accordingly.
        This method should be cheap for program already on the device and can therefore be used
        for syncing. Programs that are uploaded should be fast(~1 sec) to arm.

        Args:
            name: A name for the program on the AWG.
            program: The program (a sequence of instructions) to upload.
            channels: Tuple of length num_channels that ChannelIDs of  in the program to use. Position in the list
            corresponds to the AWG channel
            markers: List of channels in the program to use. Position in the List in the list corresponds to
            the AWG channel
            voltage_transformation: transformations applied to the waveforms extracted rom the program. Position
            in the list corresponds to the AWG channel
            force: If a different sequence is already present with the same name, it is
                overwritten if force is set to True. (default = False)

        Known programs are handled in host memory most of the time. Only when uploading the
        device memory is touched at all.

        Returning from setting user register in seqc can take from 50ms to 60 ms. Fluctuates heavily. Not a good way to
        have deterministic behaviour "setUserReg(PROG_SEL, PROG_IDLE);".
        """
        logger = logging.getLogger('ziHDAWG')
        t0=time.time()
        
        if len(channels) != self.num_channels:
            raise HDAWGValueError('Channel ID not specified')
        if len(markers) != self.num_markers:
            raise HDAWGValueError('Markers not specified')
        if len(voltage_transformation) != self.num_channels:
            raise HDAWGValueError('Wrong number of voltage transformations')

        if name in self.programs and not force:
            raise HDAWGValueError('{} is already known on {}'.format(name, self.identifier))

        # Go to qupulse nanoseconds time base.
        q_sample_rate = time_from_fraction(self.sample_rate, 10**9)

        # Adjust program to fit criteria.
        make_compatible(program,
                        minimal_waveform_length=16,
                        waveform_quantum=8,  # 8 samples for single, 4 for dual channel waaveforms.
                        sample_rate=q_sample_rate)

        # TODO: Implement offset handling like in tabor driver.
        channel_ranges = tuple([self._device.range(self._channels[ii]) for ii in range(self.num_channels)])
        logger.info(f'HDAWGChannelPair: call _program_manager.register {time.time()-t0:.2f} [s]')
        self._program_manager.register(name,
                                       program,
                                       channels,
                                       markers,
                                       voltage_transformation,
                                       q_sample_rate,
                                       channel_ranges,
                                       (0.,)*self.num_channels,
                                       force)

        logger.info(f'HDAWGChannelPair: call assemble_sequencer_program {time.time()-t0:.2f} [s]')
        awg_sequence = self._program_manager.assemble_sequencer_program()
        logger.info(f'HDAWGChannelPair: call _upload_sourcestring {time.time()-t0:.2f} [s]')
        logger.debug('-----------')
        logger.debug(awg_sequence)
        logger.debug('-----------')
        self._upload_sourcestring(awg_sequence)
        logger.info(f'HDAWGChannelPair: upload complete {time.time()-t0:.2f} [s]')

    def _upload_sourcestring(self, sourcestring: str) -> None:
        """Transfer AWG sequencer program as string to HDAWG and block till compilation and upload finish.
        Allows upload without access to data server file system."""
        if not sourcestring:
            raise HDAWGTypeError('sourcestring must not be empty or compilation will not start.')
        logger = logging.getLogger('ziHDAWG')

        # Transfer the AWG sequence program. Compilation starts automatically if sourcestring is set.
        self.awg_module.set('awgModule/compiler/sourcestring', sourcestring)
        self._poll_compile_and_upload_finished(logger)

    def _poll_compile_and_upload_finished(self, logger: logging.Logger) -> None:
        """Blocks till compilation on data server and upload to HDAWG succeed,
        if process takes less time than timeout."""
        time_start = time.time()
        logger.info('Compilation started')
        while self.awg_module.getInt('awgModule/compiler/status') == -1:
            time.sleep(0.1)
        if time.time() - time_start > self._timeout:
            raise HDAWGTimeoutError("Compilation timeout out")

        if self.awg_module.getInt('awgModule/compiler/status') == 1:
            msg = self.awg_module.getString('awgModule/compiler/statusstring')
            logger.error(msg)
            raise HDAWGCompilationException(msg)

        if self.awg_module.getInt('awgModule/compiler/status') == 0:
            logger.info('Compilation successful')
        if self.awg_module.getInt('awgModule/compiler/status') == 2:
            msg = self.awg_module.getString('awgModule/compiler/statusstring')
            logger.warning(msg)

        i = 0
        while ((self.awg_module.getDouble('awgModule/progress') < 1.0) and
               (self.awg_module.getInt('awgModule/elf/status') != 1)):
            time.sleep(0.2)
            logger.info("{} awgModule/progress: {:.2f}".format(i, self.awg_module.getDouble('awgModule/progress')))
            i = i + 1
            if time.time() - time_start > self._timeout:
                raise HDAWGTimeoutError("Upload timeout out after {self._timeout} seconds")
        logger.info("{} awgModule/progress: {:.2f}".format(i, self.awg_module.getDouble('awgModule/progress')))

        if self.awg_module.getInt('awgModule/elf/status') == 0:
            logger.info('Upload to the instrument successful')
            logger.info('Process took {:.3f} seconds'.format(time.time()-time_start))
        if self.awg_module.getInt('awgModule/elf/status') == 1:
            raise HDAWGUploadException()

    def remove(self, name: str) -> None:
        """Remove a program from the AWG.

        Also discards all waveforms referenced only by the program identified by name.

        Args:
            name: The name of the program to remove.
        """
        self._program_manager.remove(name)

    def clear(self) -> None:
        """Removes all programs and waveforms from the AWG.

        Caution: This affects all programs and waveforms on the AWG, not only those uploaded using qupulse!
        """
        self._program_manager.clear()
        self._wave_manager.clear()
        self._current_program = None

    def arm(self, name: Optional[str]) -> None:
        """Load the program 'name' and arm the device for running it. If name is None the awg will "dearm" its current
        program."""
        if not name:
            self.user_register(HDAWGRegisterFunc.PROG_SEL.value, HDAWGRegisterFunc.PROG_IDLE.value)
            self._current_program = None
        else:
            if name not in self.programs:
                raise HDAWGValueError('{} is unknown on {}'.format(name, self.identifier))
            self._current_program = name
            self.user_register(HDAWGRegisterFunc.PROG_SEL.value, self._program_manager.name_to_index(name))

    def run_current_program(self) -> None:
        """Run armed program."""
        # TODO: playWaveDigTrigger() + digital trigger here, alternative implementation.
        if self._current_program is not None:
            if self._current_program not in self.programs:
                raise HDAWGValueError('{} is unknown on {}'.format(self._current_program, self.identifier))
            if self.enable():
                self.enable(False)
            self.enable(True)
        else:
            raise HDAWGRuntimeError('No program active')

    @property
    def programs(self) -> Set[str]:
        """The set of program names that can currently be executed on the hardware AWG."""
        return self._program_manager.programs()

    @property
    def sample_rate(self) -> TimeType:
        """The default sample rate of the AWG channel group."""
        node_path = '/{}/awgs/{}/time'.format(self.device.serial, self.awg_group_index)
        sample_rate_num = self.device.api_session.getInt(node_path)
        node_path = '/{}/system/clocks/sampleclock/freq'.format(self.device.serial)
        sample_clock = self.device.api_session.getDouble(node_path)

        """Calculate exact rational number based on (sample_clock Sa/s) / 2^sample_rate_num. Otherwise numerical
        imprecision will give rise to errors for very long pulses. fractions.Fraction does not accept floating point
        numerator, which sample_clock could potentially be."""
        return time_from_fraction(sample_clock, 2 ** sample_rate_num)

    @property
    def awg_group_index(self) -> int:
        """AWG node group index. Ranges: 0-3 in 4x2 mode, 0-1 in 2x4 mode, 0 in 1x8 mode"""
        return (self._channels[0] - 1) // self.num_channels

    @property
    def device(self) -> HDAWGRepresentation:
        """Reference to HDAWG representation."""
        return self._device

    @property
    def awg_module(self) -> zhinst.ziPython.AwgModule:
        """Each AWG channel group has its own awg module to manage program compilation and upload."""
        return self._awg_module

    @property
    def user_directory(self) -> str:
        """LabOne user directory with subdirectories: "awg/src" (seqc sourcefiles), "awg/elf" (compiled AWG binaries),
        "awag/waves" (user defined csv waveforms)."""
        return self.awg_module.getString('awgModule/directory')

    def enable(self, status: bool = None) -> bool:
        """Start the AWG sequencer."""
        # There is also 'awgModule/awg/enable', which seems to have the same functionality.
        node_path = '/{}/awgs/{:d}/enable'.format(self.device.serial, self.awg_group_index)
        if status is not None:
            self.device.api_session.setInt(node_path, int(status))
            self.device.api_session.sync()  # Global sync: Ensure settings have taken effect on the device.
        return bool(self.device.api_session.getInt(node_path))

    def user_register(self, reg: int, value: int = None) -> int:
        """Query user registers (1-16) and optionally set it."""
        if reg not in range(1, 17):
            raise HDAWGValueError('{} not a valid (1-16) register.'.format(reg))
        node_path = '/{}/awgs/{:d}/userregs/{:d}'.format(self.device.serial, self.awg_group_index, reg-1)
        if value is not None:
            self.device.api_session.setInt(node_path, value)
            self.device.api_session.sync()  # Global sync: Ensure settings have taken effect on the device.
        return self.device.api_session.getInt(node_path)

    def amplitude(self, channel: int, value: float = None) -> float:
        """Query AWG channel amplitude value and optionally set it. Amplitude in units of full scale of the given
         AWG Output. The full scale corresponds to the Range voltage setting of the Signal Outputs."""
        if channel not in (1, 2):
            raise HDAWGValueError('{} not a valid (1-2) channel.'.format(channel))
        node_path = '/{}/awgs/{:d}/outputs/{:d}/amplitude'.format(self.device.serial, self.awg_group_index, channel-1)
        if value is not None:
            self.device.api_session.setDouble(node_path, value)
            self.device.api_session.sync()  # Global sync: Ensure settings have taken effect on the device.
        return self.device.api_session.getDouble(node_path)

# backwards compat
HDAWGChannelPair = HDAWGChannelGroup

class HDAWGWaveManager:
    """Manages waveforms in memory and I/O of sampled data to disk."""
    # TODO: Manage references and delete csv file when no program uses it.
    # TODO: Voltage to -1..1 range and check if max amplitude in range+offset window.
    # TODO: Manage side effects if reusing data over several programs and a shared waveform is overwritten.

    def __init__(self, user_dir: str, awg_identifier: str) -> None:
        self._wave_by_data = dict()  # type: Dict[int, str]
        self._marker_by_data = dict()  # type: Dict[int, str]
        self._file_type = 'csv'
        self._awg_prefix = awg_identifier
        self._wave_dir = Path(user_dir).joinpath('awg', 'waves')
        if not self._wave_dir.is_dir():
            raise HDAWGIOError('{} does not exist or is not a directory'.format(self._wave_dir))
        self.clear()

    def clear(self) -> None:
        """Clear all known waveforms from memory and disk associated with this AWG identifier."""
        self._wave_by_data.clear()
        self._marker_by_data.clear()
        for wave_file in self._wave_dir.glob(self._awg_prefix + '_*.' + self._file_type):
            wave_file.unlink()

    def remove(self, name: str) -> None:
        """Remove one waveform from memory and disk."""
        # TODO: Inefficient call & does not care about side-effects, if wave or marker used elsewhere.
        for wf_entry, wf_name in self._wave_by_data.items():
            if wf_name == name:
                wave_path = self.full_file_path(name)
                wave_path.unlink()
                del self._wave_by_data[wf_entry]
                break

        for wf_entry, wf_name in self._marker_by_data.items():
            if wf_name == name:
                wave_path = self.full_file_path(name)
                wave_path.unlink()
                del self._marker_by_data[wf_entry]
                break

    def full_file_path(self, name: str) -> Path:
        """Absolute file path of waveform on disk."""
        return self._wave_dir.joinpath(name + '.' + self._file_type)

    def to_file(self, name: str, wave_data: np.ndarray, fmt: str = '%f', overwrite: bool = False) -> None:
        """Save sampled data to disk."""
        file_path = self.full_file_path(name)
        if file_path.is_file() and not overwrite:
            raise HDAWGIOError('{} already exists'.format(file_path))
        np.savetxt(file_path, wave_data, fmt=fmt, delimiter=' ')

    def calc_hash(self, data: np.ndarray) -> int:
        """Calculate hash of sampled data."""
        return hash(bytes(data))

    def generate_wave_name(self, data_hash: int):
        """Unique name of wave data."""
        return self._awg_prefix + '_' + str(abs(data_hash))

    def generate_marker_name(self, data_hash: int):
        """Unique name of marker data."""
        return self._awg_prefix + '_M_' + str(abs(data_hash))

    def volt_to_amp(self, volt: np.ndarray, rng: float, offset: float) -> np.ndarray:
        """Scale voltage pulse data to dimensionless -1..1 amplitude of full range. If out of range throw error."""
        # TODO: Is offset included or excluded from rng?
        if np.any(np.abs(volt-offset) > rng):
            max_volt = np.max(np.abs(volt-offset))
            raise HDAWGValueError(f'Voltage {max_volt} out of range {rng}')
        return (volt-offset)/rng

    def register(self, waveform: Waveform,
                 channels: Tuple[Optional[ChannelID], Optional[ChannelID]],
                 markers: Tuple[Optional[ChannelID], Optional[ChannelID]],
                 voltage_transformation: Tuple[Callable, Callable],
                 sample_rate: TimeType,
                 output_range: Tuple[float],
                 output_offset: Tuple[float],
                 overwrite: bool = False) -> Tuple[str, str]:
        """Write sampled waveforms from all channels in a single file and write all marker channels in a different
        single file.

         Return a tuple with the name of the waveforms file (first element) and/or markers data file (second element).
         If a waveform or marker is not present, None will be returned in place of the name.
         The sampled waveforms files and markers files are cached based on a data hash value. If a wave or marker file
          with the same hash already exists, it will reuse the existing file and return its name."""
        sample_times, n_samples = get_sample_times(waveform, sample_rate_in_GHz=sample_rate)

        number_of_channels = len(channels)
        if number_of_channels>1:
            warnings.warn('not tested?')

        if np.any([chan is not None for chan in channels]):
            amplitude = np.zeros((n_samples, number_of_channels), dtype=float)
            for idx, chan in enumerate(channels):
                if chan is not None:
                    voltage = voltage_transformation[idx](waveform.get_sampled(chan, sample_times))
                    amplitude[:, idx] = self.volt_to_amp(voltage, output_range[idx], output_offset[idx])

            # Reuse sampled data, if available.
            amplitude_hash = self.calc_hash(amplitude)
            wave_name = self._wave_by_data.get(amplitude_hash)
            if wave_name is None:
                wave_name = self.generate_wave_name(amplitude_hash)
                self._wave_by_data[amplitude_hash] = wave_name
            if overwrite or not self.full_file_path(wave_name).exists():
                self.to_file(wave_name, amplitude, overwrite=overwrite)
        else:
            wave_name = None

        if np.any([marker is not None for marker in markers]):
            marker_output = np.zeros((n_samples, number_of_channels), dtype=np.uint8)
            for idx, marker in enumerate(markers):
                if marker is not None:
                    marker_output[:, idx] = waveform.get_sampled(marker, sample_times) != 0

            marker_hash = self.calc_hash(marker_output)
            marker_name = self._marker_by_data.get(marker_hash)
            if marker_name is None:
                marker_name = self.generate_marker_name(marker_hash)
                self._marker_by_data[marker_hash] = marker_name
            if overwrite or not self.full_file_path(marker_name).exists():
                self.to_file(marker_name, marker_output, fmt='%d', overwrite=overwrite)
        else:
            marker_name = None

        return wave_name, marker_name

    def register_single_channels(self, waveform: Waveform,
                                 channels: Tuple[Optional[ChannelID], Optional[ChannelID]],
                                 markers: Tuple[Optional[ChannelID], Optional[ChannelID]],
                                 voltage_transformations: Tuple[Callable, Callable],
                                 sample_rate: TimeType,
                                 output_range: Tuple[float],
                                 output_offset: Tuple[float],
                                 overwrite: bool = False) -> Sequence[tuple]:
        """Write sampled waveforms in one file per channel and write all markers channels in separate files.
        Return a list with tuples. Each entry in the list represents one channel and contains:
        (wave_name, marker_name, hardware_channel_index, channel_name)
        """
        logger = logging.getLogger('ziHDAWG')
        registered_names = []
        for idx, channel in enumerate(channels):
            qupulse_channel_name = channel
            qupulse_marker_name = markers[idx]
            zi_channel_idx = idx+1
            
            if qupulse_channel_name is None and qupulse_marker_name is None:
                continue
            logger.debug(f'register_single_channels: register template channel {channel} to physical channel {idx+1}')

            marker = markers[idx]
            (wave_name, marker_name) = self.register(waveform,
                                                     channels=(channel,),
                                                     markers=(marker,),
                                                     voltage_transformation=(voltage_transformations[idx],),
                                                     sample_rate=sample_rate,
                                                     output_range=output_range,
                                                     output_offset=output_offset,
                                                     overwrite=overwrite)
            registered_names.append((wave_name, marker_name, zi_channel_idx, qupulse_channel_name))

        return registered_names

class HDAWGProgramManager:
    """Manages qupulse programs in memory and seqc representations of those. Facilitates qupulse to seqc translation."""

    # Unfortunately this is the 3.5 compatible way
    ProgramEntry = NamedTuple('ProgramEntry',
                              [('program', Loop),
                               ('index', int),
                               ('seqc_rep', str)]
                              )
    ProgramEntry.__doc__ = """Entry of known programs."""
    ProgramEntry.index.__doc__ = """Program to seqc switch case mapping."""
    ProgramEntry.seqc_rep.__doc__ = """Seqc representation of program inside case statement."""

    def __init__(self, wave_manager: HDAWGWaveManager, number_of_channels) -> None:
        # Use ordered dict, so index creation for new programs is trivial (also in case of deletions).
        self._known_programs = OrderedDict()  # type: Dict[str, HDAWGProgramManager.ProgramEntry]
        self._wave_manager = weakref.proxy(wave_manager)
        # TODO: Overwritten by register and used in waveform_to_seqc. This pattern is ugly. Think of something better.
        self._number_of_channels = number_of_channels
        self._channels = (None,)*self._number_of_channels
        self._markers = (None,)*self._number_of_channels
        self._voltage_transformation = (None,)*self._number_of_channels
        self._sample_rate = TimeType()
        self._overwrite = False
        self._output_range = (1,)*self._number_of_channels
        self._output_offset = (0,)*self._number_of_channels

    def remove(self, name: str) -> None:
        # TODO: Call removal of program waveforms on WaveManger.
        self._known_programs.pop(name)

    def clear(self) -> None:
        self._known_programs.clear()

    def programs(self) -> Set[str]:
        return set(self._known_programs.keys())

    def name_to_index(self, name: str) -> int:
        return self._known_programs[name].index

    def register(self, name: str,
                 program: Loop,
                 channels: Tuple[Optional[ChannelID], Optional[ChannelID]],
                 markers: Tuple[Optional[ChannelID], Optional[ChannelID]],
                 voltage_transformation: Tuple[Callable, Callable],
                 sample_rate: TimeType,
                 output_range: Tuple[float],
                 output_offset: Tuple[float],
                 overwrite: bool = False) -> None:
        self._channels = channels
        self._markers = markers
        self._voltage_transformation = voltage_transformation
        self._sample_rate = sample_rate
        self._overwrite = overwrite
        self._output_range = output_range
        self._output_offset = output_offset

        seqc_gen = self.program_to_seqc(program)
        self._known_programs[name] = self.ProgramEntry(program,
                                                       self.generate_program_index(),
                                                       '\n'.join(seqc_gen))

    def assemble_sequencer_program(self) -> str:
        awg_sequence = HDAWGProgramManager.sequencer_template.replace('_upload_time_', time.strftime('%c'))
        awg_sequence = awg_sequence.replace('_analog_waveform_block_', '')  # Not used yet.
        awg_sequence = awg_sequence.replace('_marker_waveform_block_', '')  # Not used yet.
        return awg_sequence.replace('_case_block_', self.assemble_case_block())

    def generate_program_index(self) -> int:
        """Index generation for name <-> index mapping."""
        if self._known_programs:
            last_program = next(reversed(self._known_programs))
            last_index = self._known_programs[last_program].index
            return last_index+1
        else:
            return 1  # First index of programs. 0 reserved for idle pulse.

    def program_to_seqc(self, prog: Loop) -> Iterator[str]:
        # TODO: Improve performance, by not creating temporary variable each time.
        if prog.repetition_count > 1:
            template = '  {}'
            yield 'repeat({:d}) {{'.format(prog.repetition_count)
        else:
            template = '{}'

        if prog.is_leaf():
            yield template.format(self.waveform_to_seqc(prog.waveform))
        else:
            for child in prog.children:
                for line in self.program_to_seqc(child):
                    yield template.format(line)
        if prog.repetition_count > 1:
            yield '}'

    def waveform_to_seqc(self, waveform: Waveform) -> str:
        """Return command that plays waveform."""

        logger = logging.getLogger('ziHDAWG')
        registered_names = self._wave_manager.register_single_channels(waveform,
                                                                       self._channels,
                                                                       self._markers,
                                                                       self._voltage_transformation,
                                                                       self._sample_rate,
                                                                       self._output_range,
                                                                       self._output_offset,
                                                                       self._overwrite)

        www = []
        for wave_data in registered_names:
            wf_name, mk_name, channel_idx, qupulse_channel_name = wave_data

            if mk_name is not None and wf_name is not None:
                combined_name = f'add("{wf_name}", "{mk_name}")'
            elif mk_name is not None and wf_name is None:
                combined_name = '"' + mk_name + '"'
            elif mk_name is None and wf_name is not None:
                combined_name = '"' + wf_name + '"'
            else:
                continue

            www.append(f'{channel_idx},{combined_name}')

        if not www:
            return ''

        playwave_content = ', '.join(www)
        return f'playWave({playwave_content});'

    # noinspection PyMethodMayBeStatic
    def case_wrap_program(self, prog: ProgramEntry, prog_name, indent: int = 8) -> str:
        indented_seqc = textwrap.indent('{}\n// end of {}'.format(prog.seqc_rep, prog_name), ' ' * 4)
        case_str = 'case {:d}: // Program name: {}\n{}'.format(prog.index, prog_name, indented_seqc)
        return textwrap.indent(case_str, ' ' * indent)

    def assemble_case_block(self) -> str:
        case_block = []
        for name, entry in self._known_programs.items():
            case_block.append(self.case_wrap_program(entry, name))
        return '\n'.join(case_block)

    # TODO: Make sure waitWave is really not required.
    # TODO: Is prefetching useful?
    # Structure of sequencer program.
    sequencer_template = textwrap.dedent("""\
        //////////  qupulse sequence (_upload_time_) //////////
        
        const PROG_SEL = 0; // User register for switching current program.

        // Start of analog waveform definitions.
        wave idle = zeros(16); // Default idle waveform.
        _analog_waveform_block_

        // Start of marker waveform definitions.
        _marker_waveform_block_

        // Arm program switch.
        var prog_sel = getUserReg(PROG_SEL);
        
        // Main loop.
        while(true){
            switch(prog_sel){
        _case_block_
                default:
                    playWave(1, idle);
            }
        }
        """)


class HDAWGException(Exception):
    """Base exception class for HDAWG errors."""
    pass


class HDAWGValueError(HDAWGException, ValueError):
    pass


class HDAWGTypeError(HDAWGException, TypeError):
    pass


class HDAWGRuntimeError(HDAWGException, RuntimeError):
    pass


class HDAWGIOError(HDAWGException, IOError):
    pass


class HDAWGTimeoutError(HDAWGException, TimeoutError):
    pass


class HDAWGCompilationException(HDAWGException):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self) -> str:
        return "Compilation failed: {}".format(self.msg)


class HDAWGUploadException(HDAWGException):
    def __str__(self) -> str:
        return "Upload to the instrument failed."


if __name__ == "__main__":
    from qupulse.pulses import TablePT, SequencePT, RepetitionPT
    if 0:
        hdawg = HDAWGRepresentation(device_serial='DEV8049') # , device_interface='USB')
        hdawg.reset()
    else:
        hdawg = HDAWGRepresentation(device_serial='DEV8049', channel_grouping=HDAWGChannelGrouping.CHAN_GROUP_1x8) # , device_interface='USB')
        hdawg.sample_rate_index=0
        hdawg.reset()
    
    channel_name= 'pulseA'
    entry_list1 = [(0, 0), (5, .2, 'hold'), (40, .3, 'linear'), (80, 0, 'jump')]
    entry_list2 = [(0, 0), (20, -.2, 'hold'), (40, -.3, 'linear'), (50, 0, 'jump')]
    entry_list3 = [(0, 0), (20, -.2, 'linear'), (50, -.3, 'linear'), (70, 0, 'jump')]
    tpt1 = TablePT({channel_name: entry_list1, 1: entry_list2}, measurements=[('m', 20, 30)])
    tpt2 = TablePT({channel_name: entry_list2, 1: entry_list1})
    tpt3 = TablePT({channel_name: entry_list3, 1: entry_list2}, measurements=[('m', 10, 50)])
    if 0:
        rpt = RepetitionPT(tpt1, 4)
        spt = SequencePT(tpt2, rpt)
        rpt2 = RepetitionPT(spt, 2)
        spt2 = SequencePT(rpt2, tpt3)
    else:
        tpt1 = TablePT({channel_name: entry_list1, 1: entry_list2}, measurements=[('m', 20, 30)])
        tpt1 = TablePT({channel_name: entry_list1})
        from projects.qi.utils import plot_pulse
        plot_pulse(tpt1, fig=10)
        spt2=tpt1
    p = spt2.create_program()
    print(p)

# FIXME: silent error with voltage to high?
    
    if hdawg.channel_grouping==HDAWGChannelGrouping.CHAN_GROUP_1x8:
        if 0:
            ch = [channel_name,None,channel_name,None,None,None,None,None,]
            mk = (None, None,channel_name,None,None,None,None,channel_name)
            vt = (lambda x: x, lambda x: x/10.)+(lambda x: x,)*6
            hdawg.channel_group(0).upload('table_pulse_test6', p, ch, mk, vt)
        else:
            ch = (None,)*4+('P2', 'P1', 'P2', 'P2')
            mk = ('marker', None,None)+(None,)*5
            vt = (lambda x: x/4.,)*8
            
            
            hdawg8_logger = logging.getLogger('ziHDAWG')
            hdawg8_logger.setLevel(logging.INFO)
            
            pulse_generator.clear()
            pulse_generator.experiment_sequence['compensation']['duration']=250e-6
            pulse_generator.add_stages(['initialization', 'manipulation','readout','compensation' ])
            pulse_generator.add_wait_pulse(20e-6)
            pulse_generator.add_H(qubit=0)
            pulse_generator.add_wait_pulse(20e-6)
            pulse_generator.add_CZ(0,1)
            pulse_generator.add_wait_pulse(20e-6)
            gates_pulse, iq_pules = pulse_generator.generate_pulse_sequence()
            plot_pulse(gates_pulse, 1)
            
            p=gates_pulse.create_program()
            hdawg.channel_group(0).upload('gates_test7', p, ch, mk, vt)
            
            time.sleep(.1)
            start_groups(qcodes_awg)
            select_program(qcodes_awg, 1)
            enable_outputs(qcodes_awg, 1)

            
        
    else:
        ch = (channel_name, None)
        mk = (channel_name, None)
        vt = (lambda x: x, lambda x: x)
        hdawg.channel_group(0).upload('table_pulse_test6', p, ch, mk, vt)
        

        if 0:    
            entry_list_zero = [(0, 0), (100, 0, 'hold')]
            entry_list_step = [(0, 0), (50, .5, 'hold'), (100, 0, 'hold')]
            marker_start = TablePT({'P1': entry_list_zero, 'marker': entry_list_step})
            tpt1 = TablePT({'P1': entry_list_zero, 'marker': entry_list_zero})
            spt2 = SequencePT(marker_start, tpt1)
        
    
            p = spt2.create_program()
        
            ch = ('P1', None)
            mk = ('marker', None)
            voltage_transform = (lambda x: x,) * len(ch)
            hdawg.channel_group(0).upload('table_pulse_test2', p, ch, mk, voltage_transform)

#%%
            
            #print(hdawg.get_status_table()[0])

