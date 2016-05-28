'''
Created on 2015_08_04

@author: Admir Resulaj

This protocol implements a passive odor paradigm for the Voyeur/Arduino 
platform. This includes the protocol behaviour as well as visualization (GUI).
'''

# Python library imports
from numpy import append, arange, hstack, nan, isnan, copy, negative
from copy import deepcopy
import time, os
from numpy.random import permutation  #numpy >= 1.7 for choice function
from random import choice, randint, shuffle, seed, random
from datetime import datetime
from configobj import ConfigObj
from itertools import chain
from PyQt4.QtCore import QThread

# Voyeur imports
import voyeur.db as db
from voyeur.monitor import Monitor
from voyeur.protocol import Protocol, TrialParameters, time_stamp
from voyeur.exceptions import SerialException, ProtocolException

# Olfactometer module
from olfactometer_arduino import Olfactometers

# Utilities
from Stimulus import LaserStimulus, LaserTrainStimulus  # OdorStimulus
from range_selections_overlay import RangeSelectionsOverlay
from Voyeur_utilities import save_data_file, parse_rig_config, find_odor_vial

# Enthought's traits imports (For GUI) - Place these imports under
#   voyeur imports since voyeur will select the GUI toolkit to be QT
#   By default traits will pick wx as the GUI toolkit. By importing voyeur
#   first, QT is set and used subsequently for all gui related things
from traits.trait_types import Button
from traits.api import Int, Str, Array, Float, Enum, Bool, Range,\
                                Instance, HasTraits, Trait, Dict, DelegatesTo
from traitsui.api import View, Group, HGroup, VGroup, Item, spring, Label
from chaco.api import ArrayPlotData, Plot, VPlotContainer,\
                                DataRange1D
from enable.component_editor import ComponentEditor
from enable.component import Component
from traitsui.editors import ButtonEditor, DefaultOverride
from pyface.timer.api import Timer, do_after
from pyface.api import FileDialog, OK, warning, error
from chaco.tools.api import PanTool
from chaco.axis import PlotAxis
from chaco.scales.api import TimeScale
from chaco.scales_tick_generator import ScalesTickGenerator
from chaco.scales.api import CalendarScaleSystem
from traits.has_traits import on_trait_change



class Passive_odor_presentation(Protocol):
    """Protocol and GUI for a Go/No-go behavioral paradigm."""

    # Streaming plot window size in milliseconds.
    STREAM_SIZE = 5000
    
    # Number of trials in a block.
    BLOCK_SIZE = 20

    # Flag to indicate whether we have an Arduino connected. Set to 0 for
    # debugging.
    ARDUINO = 1
    
    # Number of trials in one sliding window used for continuous 
    # visualizing of session performance.
    SLIDING_WINDOW = 10
    
    # Amount of time in milliseconds for odorant vial to be ON prior to
    # trial start. This should be sufficiently large so that odorant makes it to
    # the final valve by the trial start.
    VIAL_ON_BEFORE_TRIAL = 1500
    
    # Maximum trial duration to wait for, in seconds, before we assume problems
    # in communication.
    MAX_TRIAL_DURATION = 30
    
    # Maximum duration of a sniff cleaning attempt, during which air is being
    # pushed through the nasal cavity via the sniff cannula.
    MAX_CLEAN_DURATION = 2000
    
    # Maximum number of sniff cleaning attempts.
    MAX_CLEAN_ROUNDS = 20
    
    # Number of initial Go trials to help motivating the subject to start
    # responding to trials.
    INITIAL_GO_TRIALS = 10
    
    # Mapping of stimuli categories to code sent to Arduino.
    stimuli_categories = {
                          "Odorant_on" : 1,
                          "Odorant_off": 0,
                          }
    # Dictionary of all stimuli defined (arranged by category), with each
    # category having a list of stimuli.
    stimuli = {
               stim_category: [] for stim_category in stimuli_categories.keys()
               }
    
    # Mapping of sniff phase name to code sent to Arduino.
    sniff_phases = {
                    "Inhalation": 0,
                    "Exhalation": 1,
                    }

    #--------------------------------------------------------------------------
    # Protocol parameters.
    # These are session parameters that are not sent to the controller
    # (Arduino). These may change from trial to trial. They are stored in
    # the database file for each trial.
    #--------------------------------------------------------------------------
    mouse = Int(0, label='Mouse')   # mouse number.
    rig = Str("", label='Rig')   # rig ID.
    session = Int(0, label='Session')   # session number.
    block_size = Int(20, label="Block size")
    # Air flow in sccm set by the Air MFC.
    air_flow = Float(label="Air (sccm)")
    # Nitrogen flow in sccm set by the Nitrogen MFC.
    nitrogen_flow = Float(label="N2 (sccm)")
    # Current trial odorant name.
    odorant = Str("Current odorant", label="Odor")
    
    
    # Other session parameters that do not change from trial to trial. These
    # are currently not stored in the trials table of the database file.
    stamp = Str(label='stamp')   # time stamp.
    protocol_name = Str(label='protocol')
    enable_blocks = Bool(True, label="Arrange stimuli in blocks")
    # Rewards given from start of session.
    rewards = Int(0, label="Total rewards")
    max_rewards = Int(400, label="Reward until")   # maximum rewards allowed.
    
    #-------------------------------------------------------------------------- 
    # Controller parameters.
    # These are trial parameters sent to Arduino. By default trial_number is
    # not sent to Arduino, but it is still logged in the database file.
    #--------------------------------------------------------------------------
    trial_number = Int(0, label='Trial Number')
    # Mapped trait. trial type keyword: code sent to Arduino.
    trial_type = Trait(stimuli_categories.keys()[0],
                       stimuli_categories,
                       label="Trial type")
    water_duration = Int(0, label="Water reward duration")
    final_valve_duration = Int(0, label="Final valve duration")
    trial_duration = Int(0, label="Trial duration")
    inter_trial_interval = Int(0, label='ITI in ms')
    # Amount of time in ms to not count a lick response as the trial choice.
    # If the mouse is impulsive, this prevents uninformed false alarms.
    lick_grace_period = Int(0, label="Lick grace period")
    # Maximum time to wait for a sniff phase change before assuming that
    # sniff is lost.
    max_no_sniff_time = Int(1200, label="Sniff max delay")
    # Sniff phase from the onset of which the latency of triggering the light
    # stimulation pulse/pulses is measured. Default value is "Inhalation".
    light_trigger_phase = Trait(sniff_phases.keys()[1],
                                sniff_phases,
                                label="Light onset after")
    odorant_trigger_phase = Trait(sniff_phases.keys()[0],
                                sniff_phases,
                                label="Odorant onset after")
    
    # Other trial parameters. These are not recording in the database file.
    # but are displayed and/or computed trial to trial.
    # Next trial air flow.
    next_air_flow = Float(label="Air (sccm)")
    # Next trial nitrogen flow.
    next_nitrogen_flow = Float(label="N2 (sccm)")
    # Next trial odorant name.
    next_odorant = Str("Next odorant", label="Odor")
    next_trial_number = 0
    # Reusing of the trait definition for trial_type.
    # The values will be independent but valiadation is done the same way.
    next_trial_type = trial_type
    # Used to notify the backend of voyeur when to send
    # the next trial parameters to Arduino. Recomputed every trial depending on
    # the result and iti choice.
    next_trial_start = 0
    # [Upper, lower] bounds in milliseconds when choosing an 
    # inter trial interval for trials when there was no false alarm.
    iti_bounds  = [5000, 7000]
    # [Upper, lower] bounds for random inter trial interval assignment 
    # when the animal DID false alarm. Value is in milliseconds.
    iti_bounds_false_alarm = [10000, 15000]
    # Number of trials that had loss of sniff signal.
    lost_sniff_run = Int(0, label="Total sniff clean runs")
    # Current overall session performance.
    percent_correct = Float(0, label="Total percent correct")
        
    #--------------------------------------------------------------------------
    # Variables for the event.
    # These are the trial results sent from Arduino.
    # They are stored in the database file.
    #--------------------------------------------------------------------------    
    trial_start = Int(0, label="Start of trial time stamp")
    trial_end = Int(0, label="End of trial time stamp")
    first_lick = Int(0, label="Time of first lick")
    # Time when Arduino received the parameters for the trial (in Arduino
    # elapsed time).
    parameters_received_time = Int(0,
                                   label="Time parameters were received \
                                   by Arduino")
    lost_sniff = Bool(False, label="Lost sniff signal in last trial")
    final_valve_onset = Int(0, label="Time of final valve open")
    
    
    # Current stimulus object.
    current_stimulus = Instance(LaserTrainStimulus)
    next_stimulus = Instance(LaserTrainStimulus)
    # Holds available stimulus ids that have been recycled when generating
    # unique stimulus ids to assign to each stimulus. The first element holds
    # the smallest id available for assignment.
    _available_stimulus_ids = [1]
    # Current block of stimuli. Used when stimuli is arranged in blocks.
    stimulus_block = []
    
    # Olfactometer object that has the interface and representation of the
    # odor delivery hardware.
    olfactometer = Instance(Olfactometers)
    
    # Arrays for the responses plot.
    trial_number_tick = Array
    # All response codes (results of each trial) for the session.
    responses = Array
    # Internal arrays for the plotting of results for each trial type.
    _go_trials_line = Array
    _nogo_trials_line = Array    

    # Arrays for the streaming data plots.
    iteration = Array
    sniff = Array
    lick1 = Array
    laser = Array

    # Internal indices uses for the streaming plots.
    _last_stream_index = Float(0)
    _last_lick_index = 0
    _previous_last_stream_index = 0
    
    # Running total of each trial result type.
    # Displayed on the console after each trial.
    _total_hits = 0
    _total_correct_rejections = 0
    _total_misses = 0
    _total_false_alarms = 0
    
    # Used for sliding window performance calculations.
    _sliding_window_hits = 0
    _sliding_window_correct_rejections = 0
    # values of Go trials for the current sliding window period.
    _sliding_window_go_array = []
    # Values of NoGo trials for the current sliding window period.
    _sliding_window_nogo_array = []
    
    # Time stamp of when voyeur requested the parameters to send to Arduino.
    _parameters_sent_time = float()
    # Time stamp of when voyeur sent the results for processing.
    _results_time = float()
    
    # Packets dropped as detected from the continuous data stream. 
    _unsynced_packets = 0
    
    # This is the voyeur backend monitor. It handles data acquisition, storage,
    # and trial to trial communications with the controller (Arduino).
    monitor = Instance(Monitor)
    # used as an alias for trial_number. Included because the monitor wants to
    # access the trialNumber member not trial_number. monitor will be updated
    # in the future to be more pythonesque.
    trialNumber = Int()
    
    # GUI elements.
    event_plot = Instance(Plot, label="Success Rate")
    stream_plots = Instance(Component)   # Container for the streaming plots.
    # This plot contains the continuous signals (sniff and laser currently).
    stream_plot = Instance(Plot, label="Sniff")
    # This is the plot that has the event signals (licks, etc.)
    stream_event_plot = Instance(Plot, label="Events")
    start_button = Button()
    start_label = Str('Start')
    # Used to cycle final valve ON/OFF automatically.
    auto_final_valve = Button()
    auto_final_valve_label = Str('Final valve cycling (OFF)')
    auto_final_valve_on_duration = Int(2500, label="ON time(ms)")
    auto_final_valve_off_duration = Int(2500, label="OFF")
    auto_final_valve_mode = Enum('Single', 'Continuous', 'Repeated',
                                  label="Mode")
    auto_final_valve_state = Bool(True)
    auto_final_valve_repetitions = Int(5, label="Times")
    auto_final_valve_repetitions_label = Str("Times")
    auto_final_valve_repetitions_off_time = Int(5, label="Time(ms) between"
                                                " each repetition")
    pause_button = Button()
    pause_label = Str('Pause')
    save_as_button = Button("Save as")
    olfactometer_button = Button()
    olfactometer_label = Str('Olfactometer')
    final_valve_button = Button()
    final_valve_label = Str("Final Valve (OFF)")
    water_calibrate_button = Button()
    water_calibrate_label = Str("Calibrate Water")
    water_button = Button()
    water_label = Str('Water Valve')
    clean_valve_button = Button()
    clean_valve_label = Str("Clean (OFF)")
    pulse_generator1_button = Button(label="Trigger")
    pulse_generator2_button = pulse_generator1_button
    pulse_amplitude1 = Range(low=0,
                             high=5000,
                             value=2000,
                             mode='slider',
                             enter_set=True)
    pulse_amplitude2 = pulse_amplitude1
    pulse_duration1 = Int(0, label='Pulse duration (ms)')
    pulse_duration2 = pulse_duration1
    
    
    #--------------------------------------------------------------------------
    # GUI layout
    #--------------------------------------------------------------------------
    control = VGroup(
                     HGroup(
                            Item('start_button',
                                 editor=ButtonEditor(label_value='start_label'),
                                 show_label=False),
                            Item('pause_button',
                                 editor=ButtonEditor(label_value='pause_label'),
                                 show_label=False,
                                 enabled_when='monitor.running'),
                            Item('save_as_button',
                                 show_label=False,
                                 enabled_when='not monitor.running'),
                            Item('olfactometer_button',
                                 editor=ButtonEditor(
                                            label_value='olfactometer_label'),
                                 show_label=False),
                            label='Application Control',
                            show_border=True
                            ),
                     HGroup(
                            Item('auto_final_valve',
                                 editor=ButtonEditor(
                                                     style="button",
                                                     label_value='auto_final'
                                                                '_valve_label'),
                                 show_label=False,
                                 enabled_when='not monitor.running'),
                            Item('auto_final_valve_on_duration'),
                            Item('auto_final_valve_off_duration',
                                 visible_when='auto_final_valve_mode != \
                                               "Single"'),
                            show_border=False
                            ),
                     HGroup(
                            Item('auto_final_valve_mode'),
                            spring,
                            Item('auto_final_valve_repetitions',
                                 visible_when='auto_final_valve_mode == \
                                                 "Repeated"',
                                 show_label=False),
                            Item("auto_final_valve_repetitions_label",
                                  visible_when='auto_final_valve_mode == \
                                                 "Repeated"',
                                  show_label=False,
                                  style='readonly'),
                            spring                            
                            ),
                     Item('auto_final_valve_repetitions_off_time',
                                 visible_when='auto_final_valve_mode == \
                                                 "Repeated"'),                     
                     label='',
                     show_border=True,
                     )
    
    arduino_group = VGroup(
                           HGroup(
                                  Item('final_valve_button',
                                       editor=ButtonEditor(
                                            style="button",
                                            label_value='final_valve_label'),
                                       show_label=False),
                                  Item('clean_valve_button',
                                       editor=ButtonEditor(
                                            style="button",
                                            label_value='clean_valve_label'),
                                       show_label=False),
                                  Item('water_button',
                                       editor=ButtonEditor(
                                            label_value='water_label',
                                            style="button"),
                                       show_label=False),
                                  Item('water_calibrate_button',
                                       editor=ButtonEditor(
                                            label_value='water_calibrate_label',
                                            style="button"),
                                       show_label=False),
                                  Item('water_duration')
                                  ),
                           HGroup(
                                  Item('pulse_generator1_button',
                                       label="Pulse generator 1",
                                       show_label=True,
                                       width = -50,
                                       enabled_when='not monitor.recording'),
                                  Item('pulse_amplitude1',
                                       label="Amplitude",
                                       editor=DefaultOverride(
                                                high_label='5000(mv)',
                                                low_label='0(mv)')),
                                  Item('pulse_duration1')
                                  ),
                           HGroup(
                                  Item('pulse_generator2_button',
                                       label="Pulse generator 2",
                                       width=-50,
                                       show_label=True,
                                       enabled_when='not monitor.recording'),
                                  Item('pulse_amplitude2',
                                       label="Amplitude",
                                       editor=DefaultOverride(
                                                high_label='5000(mv)',
                                                low_label='0(mv)')),
                                  Item('pulse_duration2')
                                  ),
                           label="Arduino Control",
                           show_border=True
                           )
    
    session_group = Group(
                          Item('stamp', style='readonly'),
                          Item('protocol_name', style='readonly'),
                          HGroup(
                                 Item('mouse',
                                      enabled_when='not monitor.running',
                                      width=-70),
                                 spring,
                                 Item('session',
                                      enabled_when='not monitor.running',
                                      width=-70),
                                 Item('rig',
                                      enabled_when='not monitor.running'),
                                 ),
                          HGroup(
                                 Item('enable_blocks'),
                                 spring,
                                 Item('block_size',
                                      visible_when='enable_blocks',
                                      width=-50,
                                      tooltip="Block Size",
                                      full_size=False,
                                      springy=True,
                                      resizable=False)
                                 ),
                          HGroup(
                                 Item('rewards', style='readonly'),
                                 spring,
                                 Item('max_rewards',
                                      width=-50,
                                      tooltip="Maximum number of rewarded" 
                                                   " trials")
                                 ),
                          Item('percent_correct', style='readonly'),
                          Item('lost_sniff_run', style='readonly'),
                          label='Session',
                          show_border=True
                          )
    
    current_trial_group = Group(
                                Item('trial_number', style='readonly'),
                                Item('trial_type'),
                                HGroup(
                                       Item('trial_duration'),
                                       Item('inter_trial_interval')
                                       ),
                                HGroup(
                                       Item('odorant',
                                            style='readonly',
                                            show_label=False),
                                       spring,
                                       Item('nitrogen_flow', width=-40),
                                       Item('air_flow', width=-40)
                                       ),
                                Item('max_no_sniff_time'),
                                HGroup(
                                       Item('light_trigger_phase'),
                                       Item('odorant_trigger_phase')
                                       ),
                                label='Current Trial',
                                show_border=True
                                )

    next_trial_group = Group(
                             Item('next_trial_type'),
                             HGroup(
                                    Item('next_odorant',
                                         style='readonly',
                                         show_label = False),
                                    Item('next_nitrogen_flow', width=-40),
                                    Item('next_air_flow', width=-40)
                                    ),
                             label='Next Trial',
                             show_border=True
                             )

    event = Group(
                  Item('event_plot',
                       editor=ComponentEditor(),
                       show_label=False,
                       height=150,
                       padding=-15),
                  label='Performance', padding=2,
                  show_border=True
                  )

    stream = Group(
                   Item('stream_plots',
                        editor=ComponentEditor(),
                        show_label=False,
                        height=300,
                        padding=2),
                   label='Streaming',
                   padding=2,
                   show_border=True,
                   )
    
    # Arrangement of all the graphical component groups.
    main = View(
                VGroup(
                       HGroup(control, arduino_group),
                       HGroup(session_group,
                              current_trial_group,
                              next_trial_group),
                       stream,
                       event,
                       show_labels=True,
                       ),
                title='Voyeur - Go/NoGo protocol',
                width=1024,
                height=900,
                x=30,
                y=70,
                resizable=False,
                )
    
    def _stream_plots_default(self):
        """ Build and return the container for the streaming plots."""
        
        # Two plots will be arranged vertically with no separation.
        container = VPlotContainer(bgcolor="transparent",
                                   fill_padding=False,
                                   padding=0)
        
        # TODO: Make the plot interactive (zoom, pan, re-scale)

        # Add the plots and their data to each container.
        
        # ---------------------------------------------------------------------
        # Streaming signals container plot.
        #----------------------------------------------------------------------
        # Data definiton.
        # Associate the plot data array members with the arrays that are used
        # for updating the data. iteration is the abscissa values of the plots.
        self.stream_plot_data = ArrayPlotData(iteration=self.iteration,
                                              sniff=self.sniff,
                                              laser=self.laser)
        # Create the Plot object for the streaming data.
        plot = Plot(self.stream_plot_data, padding=25,
                    padding_top=0, padding_bottom=35, padding_left=60)
        
        # Initialize the data arrays and re-assign the values to the
        # ArrayPlotData collection.
        # X-axis values/ticks. Initialize them. They are static.
        range_in_sec = self.STREAM_SIZE / 1000.0
        self.iteration = arange(0.001, range_in_sec + 0.001, 0.001)
        # Sniff data array initialization to nans.
        # This is so that no data is plotted until we receive it and append it
        # to the right of the screen.
        self.sniff = [0] * len(self.iteration)
        self.laser = [0] * len(self.iteration)
        self.stream_plot_data.set_data("iteration", self.iteration)
        self.stream_plot_data.set_data("sniff", self.sniff)
        self.stream_plot_data.set_data("laser", self.laser)
        
        # Change plot properties.
        
        # y-axis range. Change this if you want to re-scale or offset it.
        y_range = DataRange1D(low=-2500, high=2500)
        plot.fixed_preferred_size = (100, 100)
        plot.value_range = y_range
        y_axis = plot.y_axis
        y_axis.title = "Signal (mV)"
        # Make a custom abscissa axis object.
        bottom_axis = PlotAxis(plot, orientation="bottom",
                               tick_generator=ScalesTickGenerator(
                                                    scale=TimeScale(
                                                            seconds=1)))
        # TODO: change the axis mapper to be static
        plot.x_axis = bottom_axis
        plot.x_axis.title = "Time"
        plot.legend.visible = True
        plot.legend.bgcolor = "transparent"
        
        # Add the lines to the Plot object using the data arrays that it
        # already knows about.
        plot.plot(('iteration', 'sniff'), type='line', color='black',
                  name="Sniff")
        plot.plot(("iteration", "laser"), name="Laser", color="blue",
                  line_width=2)
        
        # Keep a reference to the streaming plot so that we can update it in
        # other methods.
        self.stream_plot = plot

        # If the Plot container has a plot, assign a selection mask for the
        # trial duration to it. This overlay is for a light blue trial mask
        # that will denote the time window when a trial was running.
        if self.stream_plot.plots.keys():
            first_plot_name = self.stream_plot.plots.keys()[0]
            first_plot = self.stream_plot.plots[first_plot_name][0]
            rangeselector = RangeSelectionsOverlay(component=first_plot,
                                                   metadata_name='trials_mask')
            first_plot.overlays.append(rangeselector)
            datasource = getattr(first_plot, "index", None)
            # Add the trial timestamps as metadata to the x values datasource.
            datasource.metadata.setdefault("trials_mask", [])
            # Testing code of how to include a mask. 
            # mask = [2,3]
            # datasource.metadata['trials_mask'] = mask
            # datasource.metadata_changed = {"first_plot": mask}
            # datasource.metadata_changed = {"selection_masks": (500,1500)}
        
        # ---------------------------------------------------------------------
        # Event signals container plot.
        #----------------------------------------------------------------------
        
        # This second plot is for the event signals (e.g. the lick signal).
        # It shares the same timescale as the streaming plot.
        # Data definiton.
        self.stream_events_data = ArrayPlotData(iteration=self.iteration,
                                                lick1=self.lick1)
        # Plot object created with the data definition above.
        plot = Plot(self.stream_events_data, padding=25, padding_bottom=0,
                    padding_left=60, index_mapper=self.stream_plot.index_mapper)
        
        # Data array for the lick signal.
        # The last value is not nan so that the first incoming streaming value
        # can be set to nan. Implementation detail on how we start streaming.
        self.lick1 = [nan] * len(self.iteration)
        self.lick1[-1] = 0
        self.stream_events_data.set_data("iteration", self.iteration)
        self.stream_events_data.set_data("lick1", self.lick1)
        
        # Change plot properties.
        plot.fixed_preferred_size = (100, 50)
        y_range = DataRange1D(low=0, high=3)
        plot.value_range = y_range
        plot.x_axis.orientation = "top"
        plot.x_axis.title = "Events"
        plot.x_axis.title_spacing = 5
        plot.x_axis.tick_generator = self.stream_plot.x_axis.tick_generator
        plot.legend.visible = True
        plot.legend.bgcolor = "transparent"
        # Upper left corner for the legend as the plots will scroll from the
        # right edge of the screen.
        plot.legend.align = 'ul'
        
        # Add the lines to the plot and grab one of the plot references.
        event_plot = plot.plot(("iteration", "lick1"),
                               name="Lick Events",
                               color="red", 
                               line_width=5,
                               render_style="hold")[0]
        # Add the trials overlay to the streaming events plot too.          
        event_plot.overlays.append(rangeselector)
        
        self.stream_event_plot = plot
        
        # Finally add both plot containers to the vertical plot container.
        container.add(self.stream_plot)
        container.add(self.stream_event_plot)

        return container

    def _addtrialmask(self):
        """ Add a masking overlay to mark the time windows when a trial was \
        occuring """
        
        # TODO: eventually rewrite this using signal objects that handle the
        # overlays.
        # Get the sniff plot and add a selection overlay ability to it
        if 'Sniff' in self.stream_plot.plots.keys():
            sniff = self.stream_plot.plots['Sniff'][0]
            datasource = getattr(sniff, "index", None)
            data = self.iteration
            # The trial time window has already passed through our window size.
            if self._last_stream_index - self.trial_end >= self.STREAM_SIZE:
                return
            # Part of the trial window is already beyond our window size.
            elif self._last_stream_index - self.trial_start >= self.STREAM_SIZE:
                start = data[0]
            else:
                start = data[-self._last_stream_index + self.trial_start - 1]
            end = data[-self._last_stream_index + self.trial_end - 1]
            # Add the new trial bounds to the masking overlay.
            datasource.metadata['trials_mask'] += (start, end)

    def __last_stream_index_changed(self):
        """ The end time tick in our plots has changed. Recompute signals. """

        shift = self._last_stream_index - self._previous_last_stream_index
        self._previous_last_stream_index = self._last_stream_index

        streams = self.stream_definition()
        # Nothing to display if no streaming data
        if streams == None:
            return
        # Currently this code is dependent on the sniff signal. Needs to change.
        # TODO: Uncouple the dependence on a particular signal.
        if 'sniff' in streams.keys():
            # Get the sniff plot and update the selection overlay mask.
            if 'Sniff' in self.stream_plot.plots.keys():
                sniff = self.stream_plot.plots['Sniff'][0]
                datasource = getattr(sniff, "index", None)
                mask = datasource.metadata['trials_mask']
                new_mask = []
                for index in range(len(mask)):
                    mask_point = mask[index] - shift / 1000.0
                    if mask_point < 0.001:
                        if index % 2 == 0:
                            new_mask.append(0.001)
                        else:
                            del new_mask[-1]
                    else:
                        new_mask.append(mask_point)
                datasource.metadata['trials_mask'] = new_mask
                
    def _updatelaser(self, lasertime):
        self.laser[-(self._last_stream_index - lasertime)] = self.pulse_amplitude1
        self.stream_plot_data.set_data('laser', self.laser)
        return

    def _restart(self):

        self.trial_number = 1
        self.rewards = 0
        self._go_trials_line = [1]
        self._nogo_trials_line = [1]
        self.trial_number_tick = [0]
        self.responses = [0]
        #self.iteration = [0]
        #self.lick = [0]
        self._sliding_window_go_array = []
        self._sliding_window_nogo_array = []
        self._total_hits = 0
        self._total_misses = 0
        self._total_correct_rejections = 0
        self._total_false_alarms = 0
        self._sliding_window_hits = 0
        self._sliding_window_correct_rejections = 0
        self.calculate_next_trial_parameters()
        
        time.clock()

        self.olfactometer = Olfactometers()
        if len(self.olfactometer.olfas) == 0:
            self.olfactometer = None
        self._setflows()

        return

    def _mouse_changed(self):
        new_stamp = time_stamp()
        db = 'mouse' + str(self.mouse) + '_' + 'sess' + str(self.session) \
                    + '_' + new_stamp
        if self.db != db:
            self.db = db
        return

    def _build_stimulus_set(self):

        self.stimuli["Odorant_off"] = []
        self.stimuli["Odorant_on"] = []
        
        
        self.lick_grace_period = 200 # grace period after FV open where responses are recorded but not scored.
        self.iti_bounds = [8000,10000] # ITI in ms for all responses other than FA.
        self.iti_bounds_false_alarm = [12000,15000] #ITI in ms for false alarm responses (punishment).
        
        odor_stimulus = LaserTrainStimulus(odorvalves=(find_odor_vial(self.olfas, 'pinene', 0.01)['key'][0],),  # find the vial with pinene. ASSUMES THAT ONLY ONE OLFACTOMETER IS PRESENT!
                                flows=[(0, 0)],  # [(AIR, Nitrogen)]
                                # Format: [POWER, DURATION_OF_PULSE (in us!!), DELAY FROM INHALE/EXHALE TRIGGER, CHANNEL]
                                laserstims=[(self.laser_power_table['20mW'][0], # Amplitude for the first channel
                                             10000,  # Duration in microseconds for the first channel
                                             25,  # Onset latency for the first channel
                                             1),  # Pulse hardware output channel
                                            (self.laser_power_table['20mW'][1],
                                             10000,
                                             25,
                                             2)],
                                #example: [self.laser_power_table['20mW'][1], laserduration, delay, 2]
                                id = 1,
                                description="Odorant stimulus",
                                num_lasers=2,  # the number of channels that the arduino should look for in laserstims.
                                numPulses=[1, 1],  # number of pulses that you want.
                                pulseOffDuration=[500, 500],  # interval between pulses if you want more than one pulse.
                                trial_type = "Odorant_on"
                                )
        no_odor_stimulus = LaserTrainStimulus(odorvalves=(find_odor_vial(self.olfas, 'Ethyl_Tiglate', 0.01)['key'][0],),  # find the vial with pinene. ASSUMES THAT ONLY ONE OLFACTOMETER IS PRESENT!
                                flows=[(0, 0)],  # [(AIR, Nitrogen)] 
                                # Format: [POWER, DURATION_OF_PULSE (in us!!), DELAY FROM INHALE/EXHALE TRIGGER, CHANNEL]
                                laserstims=[],
                                id=2,
                                description="No odorant stimulus",
                                num_lasers=0,  # the number of channels that the arduino should look for in laserstims.
                                numPulses=[0,0],  # number of pulses that you want.
                                pulseOffDuration=[0,0],  # interval between pulses if you want more than one pulse.
                                trial_type = "Odorant_off"
                                )
            
        self.stimuli['Odorant_off'].append(no_odor_stimulus)
        self.stimuli['Odorant_on'].append(odor_stimulus)

        print "---------- Stimuli changed ----------"
        for stimulus in self.stimuli.values():
            for stim in stimulus:
                print stim
        print "Blocksize:", self.block_size
        print "-------------------------------------"
        return

    def _rig_changed(self):
        new_stamp = time_stamp()
        db = 'mouse' + str(self.mouse) + '_' + 'sess' + str(self.session) \
            + '_' + new_stamp
        if self.db != db:
            self.db = db
        return

    def _session_changed(self):
        new_stamp = time_stamp()
        self.db = 'mouse' + str(self.mouse) + '_' + 'sess' + str(self.session) \
            + '_' + new_stamp
    
    @on_trait_change('trial_number')        
    def update_trialNumber(self):
        """ Copy the value of trial_number when it changes into its alias \
        trialNumber.
        
        The Monitor object is currently looking for a trialNumber attribute.
        This maintains compatibility. The Monitor will be updated to a more
        pythonesque version in the near future and this method becomes then
        obsolete and will have no effect.
        """
        self.trialNumber = self.trial_number

    def _responses_changed(self):

        if len(self.responses) == 1:
            return

        self.trial_number_tick = arange(0, len(self.responses))

        gocorrect = int
        nogocorrect = int
        lastelement = self.responses[-1]
        
        if(lastelement == 1):  # HIT
            self._total_hits += 1
            if len(self._sliding_window_go_array) == self.SLIDING_WINDOW:
                if(self._sliding_window_go_array[0] != 1):
                    self._sliding_window_hits += 1
                del self._sliding_window_go_array[0]
            else:
                self._sliding_window_hits += 1
            self._sliding_window_go_array.append(lastelement)
                
        elif(lastelement == 2):  # Correct rejection
            self._total_correct_rejections += 1
            if len(self._sliding_window_nogo_array) == self.SLIDING_WINDOW:
                if(self._sliding_window_nogo_array[0] != 2):
                    self._sliding_window_correct_rejections += 1
                del self._sliding_window_nogo_array[0]
            else:
                self._sliding_window_correct_rejections += 1
            self._sliding_window_nogo_array.append(lastelement)
            
        elif(lastelement == 3):  # MISS
            self._total_misses += 1
            if len(self._sliding_window_go_array) == self.SLIDING_WINDOW:
                if(self._sliding_window_go_array[0] == 1):
                    self._sliding_window_hits -= 1
                del self._sliding_window_go_array[0]
            self._sliding_window_go_array.append(lastelement)
                
        elif(lastelement == 4):  # False alarm
            self._total_false_alarms += 1
            if len(self._sliding_window_nogo_array) == self.SLIDING_WINDOW:
                if(self._sliding_window_nogo_array[0] == 2):
                    self._sliding_window_correct_rejections -= 1
                del self._sliding_window_nogo_array[0]
            self._sliding_window_nogo_array.append(lastelement)
        
        # sliding window data arrays
        slwgotrials = len(self._sliding_window_go_array)
        if slwgotrials == 0:
            gocorrect = 1
        else:
            gocorrect = self._sliding_window_hits*1.0/slwgotrials
        
        slwnogotrials = len(self._sliding_window_nogo_array)
        if slwnogotrials == 0:
            nogocorrect = 1
        else:
            nogocorrect = self._sliding_window_correct_rejections*1.0/slwnogotrials
        
        #print "Sliding Window Correct Go Trials %: " + str(gocorrect*100) +\
        #        "\tSliding Window Correct Nogo Trials %: " + str(nogocorrect*100)
                
        self._go_trials_line = append(self._go_trials_line, gocorrect*100)
        self._nogo_trials_line = append(self._nogo_trials_line, nogocorrect*100)
        print "Hits: " + str(self._total_hits) + "\tCRs: " + str(self._total_correct_rejections) +\
         "\tMisses: " + str(self._total_misses) + "\tFAs: " + str(self._total_false_alarms)
        
        self.event_plot_data.set_data("trial_number_tick", self.trial_number_tick)        
        self.event_plot_data.set_data("_go_trials_line", self._go_trials_line)
        self.event_plot_data.set_data("_nogo_trials_line", self._nogo_trials_line)
        self.event_plot.request_redraw()

    #TODO: fix the cycle
    def _callibrate(self):
        """ Fire the final valve on and off every 2.5s.
        
        This is is a convenience method used when PIDing for automatic
        triggering of the final valve.
        """
        if self.start_label == "Start" and  self.auto_final_valve_label == "Final valve cycling (ON)":
            Timer.singleShot(self.auto_final_valve_on_duration, self._callibrate)

        self._final_valve_button_fired()

        return

    def _clean_o_matic(self, clean_duration=2000):
        """ Implements the automatic clean protocol for cleaning the mouse's nose and recovering sniff
        parameters: clean_duration is the duration in ms of cleaning period """\
        
        if clean_duration < 0 and clean_duration > self.MAX_CLEAN_DURATION:
            clean_duration = self.MAX_CLEAN_DURATION

        # fire the pause
        if self.pause_label == 'Pause' and self._sniff_cleaning == True:
            self._pause_button_fired()

        if self.pause_label == 'Unpause' and self._sniff_cleaning == False:
            self._pause_button_fired()

        # If we are in the sniff cleaning state, send the clean command to Arduino
        if self._sniff_cleaning:
            self.monitor.send_command("clean " + str(clean_duration))
            self.clean_valve_label = "Clean (ON)"
            self._sniff_cleaning = False
            # unpause in
            Timer.singleShot(1000, self._clean_o_matic)
        # else, indicate that we are not in the cleaning state by updating the button label
        else:
            self.clean_valve_label = "Clean (OFF)"

        return

#-------------------------------------------------------------------------------
#--------------------------Button events----------------------------------------
    def _start_button_fired(self):
        #self.test_data_generator.start()
        #return
        if self.monitor.running:
            self.start_label = 'Start'
            if self.olfactometer is not None:
                for i in range(self.olfactometer.deviceCount):
                    self.olfactometer.olfas[i].valves.setdummyvalve(valvestate=0)
#                    self.olfactometer.olfas[i].mfc1.setMFCrate(0)
#                    self.olfactometer.olfas[i].mfc2.setMFCrate(0)
            if self.final_valve_label == "Final Valve (ON)":
                self._final_valve_button_fired()
            self.monitor.stop_acquisition()
            print "Unsynced trials: ", self._unsynced_packets
            #save_data_file(self.monitor.database_file,self.config['serverPaths']['mountPoint']+self.config['serverPaths']['chrisw'])
        else:
            # self.session = self.session + 1
            self.start_label = 'Stop'
            self._restart()
            self._odorvalveon()
            # self._callibrate()
            self.monitor.database_file = 'C:/VoyeurData/' + self.db
            self.monitor.start_acquisition()
            # TODO: make the monitor start acquisition start an ITI, not a trial.
        return

    def _auto_final_valve_fired(self, button_not_clicked=True):
        """ Automatically cycle the final valve ON and OFF.
        
        This helps in testing or calibrating the rig impedances as the
        PID response is monitored.
        """
        
        # The status of the auto final valve. If the state is False, the user
        # requested stopping the operation via the gui/
        if not self.auto_final_valve_state:
            self.auto_final_valve_state = True
            return
        
        if not button_not_clicked and self.auto_final_valve_label == "Final "\
                                        "valve cycling (ON)":
            self.auto_final_valve_label = "Final valve cycling (OFF)"
            if self.final_valve_label == "Final Valve (ON)":
                self._final_valve_button_fired()
                self.final_valve_label = "Final Valve (OFF)"
            self.auto_final_valve_state = False
            return
        
        if self.auto_final_valve_mode == 'Repeated' and \
                self.final_valve_label == "Final Valve (ON)":
            self.auto_final_valve_repetitions -= 1
            if self.auto_final_valve_repetitions < 1:
                self.auto_final_valve_label = 'Final valve cycling (OFF)' 
                return

        if self.auto_final_valve_mode == 'Single':
            if self.final_valve_label == "Final Valve (OFF)":
                self._final_valve_button_fired()
                self.auto_final_valve_label = 'Final valve cycling (ON)'
                Timer.singleShot(self.auto_final_valve_on_duration,
                                 self._auto_final_valve_fired)
            else:
                self._final_valve_button_fired()
                self.auto_final_valve_label = 'Final valve cycling (OFF)'
        elif self.auto_final_valve_mode == 'Continuous' or \
                self.auto_final_valve_mode == 'Repeated':
            if self.final_valve_label == "Final Valve (OFF)":
                self._final_valve_button_fired()
                Timer.singleShot(self.auto_final_valve_on_duration,
                                 self._auto_final_valve_fired)
            elif self.final_valve_label == "Final Valve (ON)":
                self._final_valve_button_fired()
                Timer.singleShot(self.auto_final_valve_off_duration,
                                 self._auto_final_valve_fired)
            # At this point we are still cycling through the final valve
            self.auto_final_valve_label = 'Final valve cycling (ON)'
            
        return
    
    def _pause_button_fired(self):
        if self.monitor.recording:
            self.monitor.pause_acquisition()
            if self.olfactometer is not None:
                for i in range(self.olfactometer.deviceCount):
                    self.olfactometer.olfas[i].valves.setdummyvalve(valvestate=0)
            self.pause_label = 'Unpause'
        else:
            self.pause_label = 'Pause'
            self.trial_number = self.next_trial_number
            self.next_trial_number += 1
            self.monitor.unpause_acquisition()
        return

    def _save_as_button_fired(self):
        dialog = FileDialog(action="save as")
        dialog.open()
        if dialog.return_code == OK:
            self.db = os.path.join(dialog.directory, dialog.filename)
        return

    # Open olfactometer object here
    def _olfactometer_button_fired(self):
        if(self.olfactometer != None):
            self.olfactometer.open()
            self.olfactometer._create_contents(self)

    def _final_valve_button_fired(self):
        if self.monitor.recording:
            self._pause_button_fired()
        if self.final_valve_label == "Final Valve (OFF)":
            self.monitor.send_command("fv on")
            self.final_valve_label = "Final Valve (ON)"
        elif self.final_valve_label == "Final Valve (ON)":
            self.monitor.send_command("fv off")
            self.final_valve_label = "Final Valve (OFF)"

    def _water_button_fired(self):
        if self.monitor.recording:
            self._pause_button_fired()
        command = "wv 1 " + str(self.water_duration)
        self.monitor.send_command(command)

    def _water_calibrate_button_fired(self):
        
        if self.monitor.recording:
            self._pause_button_fired()
        command = "callibrate 1" + " " + str(self.water_duration)
        self.monitor.send_command(command)

    def _clean_valve_button_fired(self):
        if self.monitor.recording:
            self._pause_button_fired()
        if self.clean_valve_label == "Clean (OFF)":
            self.monitor.send_command("clean on")
            self.clean_valve_label = "Clean (ON)"
        elif self.clean_valve_label == "Clean (ON)":
            self.monitor.send_command("clean off")
            self.clean_valve_label = "Clean (OFF)"
        return

    def _pulse_generator1_button_fired(self):
        """ Send a laser trigger command to the Arduino, for pulse channel 1.
        """
        
        if self.monitor.recording:
            self._pause_button_fired()
        
        command = "Laser 1 trigger " + str(self.pulse_amplitude1) + " " + str(self.pulse_duration1)
        self.monitor.send_command(command)

    def _pulse_generator2_button_fired(self):
        """ Send a laser trigger command to the Arduino, for pulse channel 2.
        """
        
        if self.monitor.recording:
            self._pause_button_fired()
        
        command = "Laser 2 trigger " + str(self.pulse_amplitude2) + " " + str(self.pulse_duration2)
        self.monitor.send_command(command)


#-------------------------------------------------------------------------------
#--------------------------Initialization---------------------------------------
    def __init__(self, trial_number,
                        mouse,
                        session,
                        stamp,
                        inter_trial_interval,
                        trial_type,
                        max_rewards,
                        final_valve_duration,
                        trial_duration,
                        stimindex=0,
                        **kwtraits):
        
        super(Passive_odor_presentation, self).__init__(**kwtraits)
        self.trial_number = trial_number
        self.stamp = stamp
                
        self.db = 'mouse' + str(mouse) + '_' + 'sess' + str(session) \
                    + '_' + self.stamp     
        self.mouse = mouse
        self.session = session
        
        self.protocol_name = self.__class__.__name__
        
        #get a configuration object with the default settings.
        self.config = parse_rig_config("C:\\workspace\\sMellRI\\voyeur_rig_config.conf")
        self.rig = self.config['rigName']
        self.water_duration = self.config['waterValveDurations']['valve_1_left']['0.25ul']
        self.olfas = self.config['olfas']
        self.olfaComPort1 = 'COM' + str(self.olfas[0]['comPort'])
        self.laser_power_table = self.config['lightSource']['powerTable']

        self._build_stimulus_set()
        self.calculate_next_trial_parameters()
        self.calculate_current_trial_parameters()
        
        self.inter_trial_interval = inter_trial_interval
        self.final_valve_duration = final_valve_duration
        self.trial_duration = trial_duration
        self.lick_grace_period = lick_grace_period
        
        self.block_size = self.BLOCK_SIZE
        self.pulse_amplitude1 = laseramp
        self.rewards = 0
        self.max_rewards = max_rewards
        
        # Setup the performance plots
        self.event_plot_data = ArrayPlotData(trial_number_tick=self.trial_number_tick, _go_trials_line=self._go_trials_line,
                                             _nogo_trials_line = self._nogo_trials_line)
        plot = Plot(self.event_plot_data, padding=10, padding_top=5, padding_bottom=30, padding_left=60)
        self.event_plot = plot
        plot.plot(('trial_number_tick', '_go_trials_line'), type = 'scatter', color = 'blue',
                   name = "Go Trials % correct")
        plot.plot(('trial_number_tick', '_nogo_trials_line'), type = 'scatter', color = 'red',
                   name = "No-Go Trials % correct")
        plot.legend.visible = True
        plot.legend.bgcolor = "transparent"
        plot.legend.align = "ll"
        plot.y_axis.title = "Performance - % Correct"
        y_range = DataRange1D(low=0, high=100)
        plot.value_range = y_range
        self.trial_number_tick = [0]
        self.responses = [0]
        
        time.clock()

        self.olfactometer = Olfactometers()
        try:
            self.olfactometer.create_serial(self.olfaComPort1)
        except:
            self.olfactometer.olfas = []
        if len(self.olfactometer.olfas) == 0:
             self.olfactometer = None
        else:
             self.olfactometer.olfas[0].valves.setdummyvalve(valvestate=0)
        self._setflows()

        if self.ARDUINO:
            self.monitor = Monitor()
            self.monitor.protocol = self
        
    def trial_parameters(self):
        """Return a class of TrialParameters for the upcoming trial.
        
        Modify this method, assigning the actual trial parameter values for the
        trial so that they can be passed onto the controller and saved on the
        database file.
        """

        protocol_params = {
                   "mouse"                  : self.mouse,
                   "rig"                    : self.rig,
                   "session"                : self.session,
                   "block_size"             : self.block_size,
                   "air_flow"               : self.air_flow,
                   "nitrogen_flow"          : self.nitrogen_flow,
                   "odorant"                : self.odorant,
                   "stimulus_id"            : self.current_stimulus.id,
                   "description"            : self.current_stimulus.description,
                   "trial_category"         : self.trial_type,
                   "odorant_trigger_phase"  : self.odorant_trigger_phase,
                   }
        
        # Parameters sent to the controller (Arduino)
        controller_dict = {
               "trialNumber"          : (1, db.Int, self.trial_number),
               "final_valve_duration" : (2, db.Int, self.final_valve_duration),
               "trial_duration"       : (3, db.Int, self.trial_duration),
               "inter_trial_interval" : (4, db.Int, self.inter_trial_interval),
               "odorant_trigger_phase_code": (5, db.Int,
                                              self.odorant_trigger_phase_),
               "max_no_sniff_time"    : (6, db.Int, self.max_no_sniff_time),
                           }
   
        return TrialParameters(
                    protocolParams=protocol_params,
                    controllerParams=controller_dict
                )

    def protocol_parameters_definition(self):
        """Returns a dictionary of {name => db.type} defining protocol parameters"""

        params_def = {
            "mouse"               : db.Int,
            "rig"                 : db.String32,
            "session"             : db.Int,
            "block_size"          : db.Int,
            "air_flow"            : db.Float,
            "nitrogen_flow"       : db.Float,
            "odorant"             : db.String32,
            "stimulus_id"         : db.Int,
            "description"         : db.String32,
            "trial_category"      : db.String32,
            "odorant_trigger_phase"  : db.String32,
            "trial_category"      : db.String32
        }

        return params_def

    def controller_parameters_definition(self):
        """Returns a dictionary of {name => db.type} defining controller (Arduino) parameters"""

        params_def = {
            "trialNumber"                : db.Int,
            "final_valve_duration"       : db.Int,
            "trial_duration"             : db.Int,
            "inter_trial_interval"       : db.Int,
            "odorant_trigger_phase_code" : db.Int,
            "max_no_sniff_time"          : db.Int
        }
           
        return params_def

    def event_definition(self):
        """Returns a dictionary of {name => (index,db.Type} of event parameters for this protocol"""

        return {
            "response"                : (1, db.Int),
            "parameters_received_time": (2, db.Int),
            "trial_start"             : (3, db.Int),
            "trial_end"               : (4, db.Int),
            "lost_sniff"              : (7, db.Int),
            "final_valve_onset"       : (8, db.Int)
        }

    def stream_definition(self):
        """Returns a dictionary of {name => (index,db.Type} of streaming data parameters for this protocol"""
             
        return {
            "packet_sent_time" : (1, 'unsigned long', db.Int),
            "sniff_samples"    : (2, 'unsigned int', db.Int),
            "sniff"            : (3, 'int', db.FloatArray),
            #"sniff_ttl"        : (4, db.FloatArray),
            #"lick1"            : (4, 'unsigned long', db.FloatArray),
        }

    def process_event_request(self, event):
        """
        Process event requested from controller, run sniff clean if needed, set the parameters for the following trial and set MFCs, calculate parameters
        for the trial that occurs after that, set timer to set vial open for next trial.
        """
        self.timestamp("end")
        self.parameters_received_time = int(event['parameters_received_time'])
        self.trial_start = int(event['trial_start'])
        self.trial_end = int(event['trial_end'])
        lasertime = int(event['light_ON_time'])

        # update trials mask
        if self.trial_end > self._last_stream_index:
            self._shiftlicks(self.trial_end - self._last_stream_index)
            self._last_stream_index = self.trial_end

        if lasertime != 0:
            self._updatelaser(lasertime)

        self._addtrialmask()

#        print "****Next Trial: ",  self.trial_number
#        print "****Stimulus_process_event: ", self.current_stimulus

        self.lost_sniff = int(event['lost_sniff']) == 1
        if self.lost_sniff and self.max_no_sniff_time > 0:
            print "LOST SNIFF!!!"
            # Only pause and unpause iff running
            if self.pause_label == "Pause" and self.lost_sniff_run < self.MAX_CLEAN_ROUNDS:
                self._sniff_cleaning = True
                self._clean_o_matic()
                self.lost_sniff_run += 1

        response = int(event['response'])  # 1 is right, 2 is left, 3 left 4 right
        if (response == 1): # a hit.
            self.rewards += 1
            if self.rewards >= self.max_rewards and self.start_label == 'Stop':
                self._start_button_fired()  # ends the session if the reward target has been reached.

        if response == 4: # a false alarm
            self.inter_trial_interval = randint(self.iti_bounds_false_alarm[0],self.iti_bounds_false_alarm[1])
        else:
            self.inter_trial_interval = randint(self.iti_bounds[0],self.iti_bounds[1])
        
        
        self.responses = append(self.responses, response)
        
        #update a couple last parameters from the next_stimulus object, then make it the current_stimulus..
        self.calculate_current_trial_parameters()
        self._last_trial_type = self.trial_type
        self.trial_type = self.next_trial_type
        self.current_stimulus = deepcopy(self.next_stimulus) # set the parameters for the following trial from nextstim.
        
        #calculate a new next stim.
        self.calculate_next_trial_parameters() # generate a new nextstim for the next next trial. 
        # If actual next trial is determined by the trial that just finished, calculate next trial parameters can set current_stimulus.
        
        # use the current_stimulus parameters to calculate values that we'll record when we start the trial.
        self._setflows()
        odorvalve = self.current_stimulus.odorvalves[0]
        valveConc = self.olfas[0][odorvalve][1]
        self.air_flow = self.current_stimulus.flows[0][0]
        self.nitrogen_flow = self.current_stimulus.flows[0][1]
        self.odorant = self.olfas[0][odorvalve][0]
        self.percent_correct = (float(self.rewards) / float(self.trial_number)) * 100

        # set up a timer for opening the vial at the begining of the next trial using the parameters from current_stimulus.
        timefromtrial_end = (self._results_time - self._parameters_sent_time) * 1000 #convert from sec to ms for python generated values
        timefromtrial_end -= (self.trial_end - self.parameters_received_time) * 1.0 
        nextvalveontime = self.inter_trial_interval - timefromtrial_end - self.VIAL_ON_BEFORE_TRIAL
        self.next_trial_start = nextvalveontime + self.VIAL_ON_BEFORE_TRIAL / 2
        if nextvalveontime < 0:
            print "Warning! nextvalveontime < 0"
            nextvalveontime = 20
            self.next_trial_start = 2000
        Timer.singleShot(int(nextvalveontime), self._odorvalveon)
        # print "ITI: ", self._next_inter_trial_interval, " timer set duration: ", int(nextvalveontime)
        
        return

    def process_stream_request(self, stream):
        """
        Process stream requested from controller.
        """
        if stream:
            # newtime = time.clock()
            num_sniffs = stream['sniff_samples']
            packet_sent_time = stream['packet_sent_time']

            #print "Num sniffs:", num_sniffs

            if packet_sent_time > self._last_stream_index + num_sniffs:
                lostsniffsamples = packet_sent_time - self._last_stream_index - num_sniffs
                print "lost sniff:", lostsniffsamples
                if lostsniffsamples > self.STREAM_SIZE:
                    lostsniffsamples = self.STREAM_SIZE
                lostsniffsamples = int(lostsniffsamples)
                # pad sniff signal with last value for the lost samples first then append received sniff signal
                new_sniff = hstack((self.sniff[-self.STREAM_SIZE + lostsniffsamples:], [self.sniff[-1]] * lostsniffsamples))
                if stream['sniff'] is not None:
                    self.sniff = hstack((new_sniff[-self.STREAM_SIZE + num_sniffs:], negative(stream['sniff'])))
            else:
                if stream['sniff'] is not None:
                    new_sniff = hstack((self.sniff[-self.STREAM_SIZE + num_sniffs:], negative(stream['sniff'])))
                self.sniff = new_sniff
            self.stream_plot_data.set_data("sniff", self.sniff)
            
            if stream['lick1'] is not None or (self._last_stream_index - self._last_lick_index < self.STREAM_SIZE):
                [self.lick1] = self._process_licks(stream, ('lick1',), [self.lick1])

            if "light_ON_time" in self.event_definition().keys():
                lasershift = int(packet_sent_time - self._last_stream_index)
                if lasershift > self.STREAM_SIZE:
                    lasershift = self.STREAM_SIZE
                new_laser = hstack((self.laser[-self.STREAM_SIZE + lasershift:], [0] * lasershift))
                self.laser = new_laser
                self.stream_plot_data.set_data('laser', self.laser)

            self._last_stream_index = packet_sent_time

            # if we haven't received results by MAX_TRIAL_DURATION, pause and unpause as there was probably some problem with comm.
            if (self.trial_number > 1) and ((time.clock() - self._results_time) > self.MAX_TRIAL_DURATION) and self.pause_label == "Pause":
                print "=============== Pausing to restart Trial =============="
                # print "param_sent time: ",self._parameters_sent_time, "_results_time:", self._results_time
                self._unsynced_packets += 1
                self._results_time = time.clock()
                # Pause and unpause only iff running
                if self.pause_label == "Pause":
                    self.pause_label = 'Unpause'
                    self._pause_button_fired()
                    # unpause in 1 second
                    Timer.singleShot(1000, self._pause_button_fired)
        return



    def _process_licks(self, stream, licksignals, lickarrays):

        packet_sent_time = stream['packet_sent_time']

        # TODO: find max shift first, apply it to all licks
        maxtimestamp = int(packet_sent_time)
        for i in range(len(lickarrays)):
            licksignal = licksignals[i]
            if licksignal in stream.keys():
                streamsignal = stream[licksignal]
                if streamsignal is not None and streamsignal[-1] > maxtimestamp:
                        maxtimestamp = streamsignal[-1]
                        print "**************************************************************"
                        print "WARNING! Lick timestamp exceeds timestamp of received packet: "
                        print "Packet sent timestamp: ", packet_sent_time, "Lick timestamp: ", streamsignal[-1]
                        print "**************************************************************"
        maxshift = int(packet_sent_time - self._last_stream_index)
        if maxshift > self.STREAM_SIZE:
            maxshift = self.STREAM_SIZE - 1

        for i in range(len(lickarrays)):

            licksignal = licksignals[i]
            lickarray = lickarrays[i]

            if licksignal in stream.keys():
                if stream[licksignal] is None:
                    lickarray = hstack((lickarray[-self.STREAM_SIZE + maxshift:], [lickarray[-1]] * maxshift))
                else:
                    # print "licks: ", stream['lick'], "\tnum sniffs: ", currentshift
                    last_state = lickarray[-1]
                    last_lick_tick = self._last_stream_index
                    for lick in stream[licksignal]:
                        # print "last lick tick: ", last_lick_tick, "\tlast state: ", last_state
#                        if lick == 0:
#                            continue
                        shift = int(lick - last_lick_tick)
                        if shift <= 0:
                            if shift < self.STREAM_SIZE * -1:
                                shift = -self.STREAM_SIZE + 1
                            if isnan(last_state):
                                lickarray[shift - 1:] = [i + 1] * (-shift + 1)
                            else:
                                lickarray[shift - 1:] = [nan] * (-shift + 1)
                        # Lick timestamp exceeds packet sent time. Just change the signal state but don't shift
                        elif lick > packet_sent_time:
                            if isnan(last_state):
                                lickarray[-1] = i + 1
                            else:
                                lickarray[-1] = nan
                        else:
                            if shift > self.STREAM_SIZE:
                                shift = self.STREAM_SIZE - 1
                            lickarray = hstack((lickarray[-self.STREAM_SIZE + shift:], [lickarray[-1]] * shift))
                            if isnan(last_state):
                                lickarray = hstack((lickarray[-self.STREAM_SIZE + 1:], [i + 1]))
                            else:
                                lickarray = hstack((lickarray[-self.STREAM_SIZE + 1:], [nan]))
                            last_lick_tick = lick
                        last_state = lickarray[-1]
                        # last timestamp of lick signal change
                        self._last_lick_index = lick
                    lastshift = int(packet_sent_time - last_lick_tick)
                    if lastshift >= self.STREAM_SIZE:
                        lastshift = self.STREAM_SIZE
                        lickarray = [lickarray[-1]] * lastshift
                    elif lastshift > 0 and len(lickarray) > 0:
                        lickarray = hstack((lickarray[-self.STREAM_SIZE + lastshift:], [lickarray[-1]] * lastshift))
                if len(lickarray) > 0:
                    self.stream_events_data.set_data(licksignal, lickarray)
                    # self.stream_event_plot.request_redraw()
                    lickarrays[i] = lickarray

        return lickarrays
    
#    def _trialNumber_changed(self):
#        print "Trial number changed to: ", self.trialNumber

    def _shiftlicks(self, shift):

        if shift > self.STREAM_SIZE:
            shift = self.STREAM_SIZE - 1

        streamdef = self.stream_definition()
        if 'lick1' in streamdef.keys():
            self.lick1 = hstack((self.lick1[-self.STREAM_SIZE + shift:], self.lick1[-1] * shift))
            self.stream_events_data.set_data('lick1', self.lick1)
        return

    def start_of_trial(self):

        self.timestamp("start")
        print "***** Trial: ", self.trial_number, "\tStimulus: ", self.current_stimulus, " *****"

    def _odorvalveon(self):
        """ Turn on odorant valve """

        # print "odorant valve on time", time.clock()
        if(self.olfactometer is None) or self.start_label == 'Start' or self.pause_label == "Unpause":
            return
        # self.olfactometer.valves.setodorvalve(self._currentvial)
        for i in range(self.olfactometer.deviceCount):
            # print "current stim: ", self.current_stimulus
            olfa = self.olfas[i]
            olfavalve = olfa[self.current_stimulus.odorvalves[i]][2]
            # print "setting odorant valve: ", olfavalve
            if olfavalve != 0:
                self.olfactometer.olfas[i].valves.setodorvalve(olfavalve) #set the vial,
                if self.olfactometer.olfas[i].valves.checkedID != olfavalve: #check that the vial was set...
                    self._pause_button_fired()
                    print "Pausing, valve not set due to lockout or error, see error above."
        """olfavalve = self.olfas[0][self.current_stimulus.odorvalves[0]][2]
        self.olfactometer.olfas[0].valves.setodorvalve(olfavalve)"""
    
    
    def _setflows(self):
        """ Set MFC Flows """

        if(self.olfactometer is None):
            return

        for i in range(1, self.olfactometer.deviceCount + 1):
            self.olfactometer.olfas[i - 1].mfc1.setMFCrate(self.current_stimulus.flows[i - 1][0])
            self.olfactometer.olfas[i - 1].mfc2.setMFCrate(self.current_stimulus.flows[i - 1][1])

    def end_of_trial(self):
        # set new trial parameters
        # turn off odorant valve
        if(self.olfactometer is not None):
            for i in range(self.olfactometer.deviceCount):
                olfa = self.olfas[i]
                olfavalve = olfa[self.current_stimulus.odorvalves[i]][2]
                if olfavalve != 0:
                    self.olfactometer.olfas[i].valves.setodorvalve(olfavalve, 0)

    
    def generate_next_stimulus_block(self):
        """ Generate a block of randomly shuffled stimuli from the stimulus \
        set stored in self.stimuli.
        
        Modify this method to implement the behaviour you want for how a block
        of stimuli is chosen.
        
        """
        
        if not self.enable_blocks:
            return
        
        if len(self.stimulus_block):
            print "Warning! Current stimulus block was not empty! Generating \
                    new block..."
        
        # Generate an initial block of Go trials if needed.
        if self.INITIAL_GO_TRIALS and \
                    self.trial_number < self.INITIAL_GO_TRIALS:
            block_size = self.INITIAL_GO_TRIALS + 1 - self.trial_number
            self.stimulus_block = [self.stimuli["Odorant_on"][0]] * block_size
            return
        
        # Randomize seed from system clock.
        seed()
        if not len(self.stimuli):
            raise ValueError("Stimulus set is empty! Cannot generate a block.")
        # Grab all stimuli arrays from our stimuli sets.
        list_all_stimuli_arrays = self.stimuli.values()
        # Flatten the list so that we have a 1 dimensional array of items
        self.stimulus_block = list(chain.from_iterable(list_all_stimuli_arrays))
                
        if self.block_size/len(self.stimulus_block) > 1:
            copies = self.block_size/len(self.stimulus_block)
            self.stimulus_block *= copies
            if len(self.stimulus_block) < self.block_size:
                print "WARNING! Block size is not a multiple of stimulus set:"\
                    "\nBlock size: %d\t Stimulus set size: %d \tConstructed " \
                    " Stimulus block size: %d" \
                    %(self.block_size, len(self.stimuli.values()),
                      len(self.stimulus_block))
            
        # Shuffle the set.
        shuffle(self.stimulus_block, random)
        print "Generated new stimulus block:"
        for i in range(len(self.stimulus_block)):
            print self.stimulus_block[i]
    
    def calculate_current_trial_parameters(self):
        """ Calculate the parameters for the currently scheduled trial.
        
        This method can be used to calculate the parameters for the trial that
        will follow next. This method is called from process_event_request,
        which is automatically called after results of the previous trial are
        received.
        
        """
        
        self.trial_number = self.next_trial_number
        self.current_stimulus = self.next_stimulus
        self.trial_type = self.current_stimulus.trial_type
        self.odorant = self.next_odorant
        self.nitrogen_flow = self.next_nitrogen_flow
        self.air_flow = self.next_air_flow
        
        # For the first trial recalculate the next trial parameters again
        # so that the second trial is also prepared and ready.
        if self.trial_number == 1:
            self.calculate_next_trial_parameters()
        
        print "Current stimulus: ", self.current_stimulus
    
    def calculate_next_trial_parameters(self):
        """ Calculate parameters for the trial that will follow the currently \
        scheduled trial.
        
        The current algorithm is such that at the end of the trial:
        current parameters are assigned values of the previous trial's next
            parameters AND
        next parameters are computed for the trial following. This is where the
        next parameters are assigned.
        
        At this point the current stimulus becomes the previous trial's next 
        stimulus. This makes it possible to know in advance the currently
        scheduled trial as well as the one after that and display this
        information in advance in the GUI. If any current trial parameters
        depend on the result of the previous trial, this is not the
        place to assign these parameters. They should be assigned in 
        calculate_current_trial_parameters method, before the call to this
        method.
        
        """
        
        self.next_trial_number = self.trial_number + 1
        
        # Grab next stimulus.
        if self.enable_blocks:
            if not len(self.stimulus_block):
                self.generate_next_stimulus_block()
            self.next_stimulus = self.stimulus_block.pop()
        else:
            # Pick a random stimulus from the stimulus set of the next
            # trial type.
            if self.next_trial_number > self.INITIAL_GO_TRIALS:
                # Randomly choose a stimulus.
                self.next_stimulus = choice([stimulus] for stimulus in
                                                 self.stimuli.values())
            else:
                # Enforce the intitial Go trials rule.
                self.next_stimulus = self.stimuli["Odorant_off"][0]
            
        if self.next_trial_number <= self.INITIAL_GO_TRIALS:
            
            self.next_stimulus = self.stimuli["Odorant_on"][0]
        
        self.next_trial_type = self.next_stimulus.trial_type
        nextodorvalve = self.next_stimulus.odorvalves[0]
        self.next_odorant = self.olfas[0][nextodorvalve][0]
        self.next_air_flow = self.next_stimulus.flows[0][0]
        self.next_nitrogen_flow = self.next_stimulus.flows[0][1]

    def trial_iti_milliseconds(self):
        if self.next_trial_start:
            return self.next_trial_start
        return 0

    def timestamp(self, when):
        """ Used to timestamp events """
        if(when == "start"):
            self._parameters_sent_time = time.clock()
            # print "start timestamp ", self._parameters_sent_time
        elif(when == "end"):
            self._results_time = time.clock()
            

from numpy import array
import math

class protocolTest(QThread):
    """ Runs a thread to generate simulated data for testing a Voyeur protocol.
    """
    
    def __init__(self, protocol=None, testing=False, QObject_parent=None,
                 run_for = 1000):
        """ Initialize thread and assign the protocol object which contains
        the GUI to it.
        """
        QThread.__init__(self, QObject_parent)
        self.protocol = protocol
        self.testing = testing
        self.run_for = run_for
        self.current_time = 0
        self.time_stamp = time.clock()
        
    def run(self):
        while self.testing and self.current_time < self.run_for:
            self.test()
    
    def generate_streams(self):
        sniff = array([])
        for iteration in range(int(random()*100)):
            value = math.sin(self.current_time/10.0)*500
            sniff = append(sniff,value)
            self.current_time += 1
        #iterations = [x for x in range(int(random()*100))]
        #sniff = [math.sin(iteration) for iteration in iterations]
        return sniff
    
    def test(self):
        """ Generate testing data to see if things play nicely."""
        
        event = {
        "response"                : (1, db.Int),
        "parameters_received_time": (2, db.Int),
        "trial_start"             : (3, db.Int),
        "trial_end"               : (4, db.Int),
        "first_lick"              : (5, db.Int),
        "laserontime"             : (7, db.Int),
        "lost_sniff"              : (8, db.Int),
        "final_valve_onset"       : (9, db.Int)
        }
    
        stream = {
                "packet_sent_time" : (1, db.Int),
                "sniff_samples"    : (2, db.Int),
                "sniff"            : (3, db.FloatArray),
                "sniff_ttl"        : (4, db.FloatArray),
                "lick1"            : (5, db.FloatArray),
            }
        
        
        sniff = self.generate_streams()
        if (time.clock() - self.time_stamp)*100 < len(sniff):
            return
        self.current_time += len(sniff)
        self.time_stamp = time.clock()
        
        stream['packet_sent_time'] = self.current_time
        stream['sniff_samples'] = len(sniff)
        stream['sniff'] = sniff
        stream['sniff_ttl'] = []
        stream['lick1'] = [0] if self.current_time < 50 else None
        
        self.protocol.calculate_next_trial_parameters()
        self.protocol.calculate_current_trial_parameters()
        self.protocol.process_stream_request(stream)
        
        event['response'] = choice([1,2,3,4])
        event['parameters_received_time'] = int(time.clock()*1000)
        event['trial_start'] = int(time.clock()*1000)  
        event['laserontime'] = int(time.clock()*1000)  
        event['trial_end'] = int(time.clock()*1000)  
        event['lost_sniff'] = 0

        #protocol.process_event_request(event)        
        
#
# Main - creates a database, sends parameters to controller, stores resulting data, and generates display
#

if __name__ == '__main__':

    # arduino parameter defaults


    trial_number = 0
    trial_type = "Go"
    final_valve_duration = 500
    trial_duration = 2500
    lick_grace_period = 0
    laseramp = 1500
    max_rewards = 400


    # protocol parameter defaults
    mouse = 434  # can I make this an illegal value so that it forces me to change it????

    session = 18
    stamp = time_stamp()
    inter_trial_interval = 8000
    
    # protocol
    protocol = Passive_odor_presentation(trial_number,
                                         mouse,
                                         session,
                                         stamp,
                                         inter_trial_interval,
                                         trial_type,
                                         max_rewards,
                                         final_valve_duration,
                                         trial_duration,
                                         )        
    # Testing code when no hardware attached.
    test_data = protocolTest(protocol, True)
    protocol.test_data_generator = test_data
    # GUI
    protocol.configure_traits()