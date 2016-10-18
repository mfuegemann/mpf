# pylint: disable-msg=too-many-lines
"""Contains the base class for ball devices."""

from collections import deque

import asyncio

from mpf.devices.ball_device.hold_coil_ejector import HoldCoilEjector

from mpf.core.delays import DelayManager
from mpf.core.device_monitor import DeviceMonitor
from mpf.core.system_wide_device import SystemWideDevice
from mpf.core.utility_functions import Util


# pylint: disable-msg=too-many-instance-attributes
from mpf.devices.ball_device.pulse_coil_ejector import PulseCoilEjector


@DeviceMonitor("_state", "balls", "available_balls", "num_eject_attempts", "eject_queue", "eject_in_progress_target",
               "mechanical_eject_in_progress", "_incoming_balls", "ball_requests", "trigger_event")
class BallDevice(SystemWideDevice):

    """
    Base class for a 'Ball Device' in a pinball machine.

    A ball device is anything that can hold one or more balls, such as a
    trough, an eject hole, a VUK, a catapult, etc.

    Args: Same as Device.
    """

    config_section = 'ball_devices'
    collection = 'ball_devices'
    class_label = 'ball_device'

    _state_transitions = dict(
        invalid=['idle'],
        idle=['waiting_for_ball', 'ejecting', 'missing_balls',
              'waiting_for_ball_mechanical'],
        lost_balls=['idle'],
        missing_balls=['ball_left', 'idle'],
        waiting_for_ball=['idle', 'waiting_for_ball_mechanical'],
        waiting_for_ball_mechanical=['idle', 'waiting_for_ball',
                                     'eject_confirmed'],
        ball_left=['eject_confirmed', 'failed_confirm'],
        ejecting=['ball_left', 'failed_eject'],
        failed_eject=['eject_broken', 'ejecting'],
        eject_broken=[],
        failed_confirm=['failed_eject', 'eject_confirmed',
                        'lost_balls'],
        eject_confirmed=['idle', 'lost_balls'],
    )

    def __init__(self, machine, name):
        """Initialise ball device."""
        super().__init__(machine, name)

        self.delay = DelayManager(machine.delayRegistry)

        self.balls = 0
        """Number of balls currently contained (held) in this device."""

        self.available_balls = 0
        """Number of balls that are available to be ejected. This differes from
        `balls` since it's possible that this device could have balls that are
        being used for some other eject, and thus not available."""

        self.eject_queue = deque()
        """ Queue of three-item tuples that represent ejects this device needs
        to do.

        Tuple structure:
        [0] = the eject target device
        [1] = boolean as to whether this is a mechanical eject
        [2] = trigger event which will trigger the actual eject attempts
        """

        self.num_eject_attempts = 0
        """ Counter of how many attempts to eject the this device has tried.
         Eventually it will give up.
        """

        self.eject_in_progress_target = None
        """The device this device is currently trying to eject to.
        @type: BallDevice
        """

        self.mechanical_eject_in_progress = False
        """How many balls are waiting for a mechanical (e.g. non coil fired /
        spring plunger) eject.
        """

        self._target_on_unexpected_ball = None
        # Device will eject to this target when it captures an unexpected ball

        self._source_devices = list()
        # Ball devices that have this device listed among their eject targets

        self._blocked_eject_attempts = deque()
        # deque of tuples that holds ejects that source devices wanted to do
        # when this device wasn't ready for them
        # each tuple is (event wait queue from eject attempt event, source)

        self._state = "invalid"
        # Name of the state of this device

        self.jam_switch_state_during_eject = False

        self._eject_status_logger = None

        self._incoming_balls = deque()
        # deque of tuples that tracks incoming balls this device should expect
        # each tuple is (self.machine.clock.get_time() formatted timeout, source device)

        self.ball_requests = deque()
        # deque of tuples that holds requests from target devices for balls
        # that this device could fulfil
        # each tuple is (target device, boolean player_controlled flag)

        self.trigger_event = None

        self._idle_counted = None

        self.eject_start_time = None
        self.ejector = None

        self._ball_counter_condition = asyncio.Event(loop=self.machine.clock.loop)
        self._eject_request_condition = asyncio.Event(loop=self.machine.clock.loop)
        self._eject_success_condition = asyncio.Event(loop=self.machine.clock.loop)
        self._source_eject_failure_condition = asyncio.Event(loop=self.machine.clock.loop)
        self._source_eject_failure_retry_condition = asyncio.Event(loop=self.machine.clock.loop)
        self._incoming_ball_condition = asyncio.Event(loop=self.machine.clock.loop)

        self.machine.events.add_handler("shutdown", self.stop)

    def _initialize(self):
        """Initialize right away."""
        self._initialize_phase_2()
        self._initialize_phase_3()

        # this has to be delayed to after the switches were read
        self.machine.events.add_handler('init_phase_4',
                                        self._initialize_phase_4)

    def stop(self, **kwargs):
        if self.runner:
            self.runner.cancel()

    # Logic and dispatchers
    @asyncio.coroutine
    def _run(self):
        # state invalid
        yield from self._initial_ball_count()
        yield from self._state_idle()

    def _switch_state(self, new_state, **kwargs):
        # Changes this device to the new state (if the transition is valid)
        raise AssertionError("bad call")
        if new_state != 'invalid':

            if new_state == self._state:  # pragma: no cover
                self.log.debug("Tried to switch state. But already in state "
                               "%s", new_state)
                return

            if new_state not in self._state_transitions[self._state]:
                raise AssertionError("Cannot transition from state {} to {}"
                                     .format(self._state, new_state))

        self._state = new_state

        self.log.debug("Switching to state %s", new_state)

        if new_state not in self._state_transitions:
            raise AssertionError("Went to invalid state %s", self._state)

    def _counted_balls(self, balls, **kwargs):
        # Called when the device counts its balls and then calls the current
        # state's _counted_balls() method.
        del kwargs
        method_name = "_state_" + self._state + "_counted_balls"
        method = getattr(self, method_name, lambda *args: None)
        method(balls)

    def _target_ready(self, target, **kwargs):
        # Called whenever one of this device's target devices changes state to
        # be ready to receive balls
        del kwargs
        del target
        return
        if self._state == "idle":
            self._check_eject_queue()

    # ---------------------------- State: invalid -----------------------------
    @asyncio.coroutine
    def _initial_ball_count(self):
        # Handle initial ball count with entrance_switch. If there is a ball on the entrance_switch at boot
        # assume that we are at max capacity.
        if (self.config['entrance_switch'] and self.config['ball_capacity'] and
                self.config['entrance_switch_full_timeout'] and
                self.machine.switch_controller.is_active(self.config['entrance_switch'].name,
                                                         ms=self.config['entrance_switch_full_timeout'])):
            self.balls = self.config['ball_capacity']

        balls = yield from self._count_balls()
        self.balls = balls
        self.available_balls = balls

    # ----------------------------- State: idle -------------------------------
    @asyncio.coroutine
    def _state_idle(self):
        # Lets count the balls to see if we received ball in the meantime
        # before we start an eject with wrong initial count
        self._idle_counted = False
        balls = yield from self._count_balls()
        self._idle_counted = True

        yield from self._handle_ball_changes(balls)

        while True:
            if self._state == "eject_broken":
                return
            self._state = "idle"
            self.debug_log("Idle")
            balls = yield from self._count_balls()
            if (yield from self._handle_ball_changes(balls)):
                continue

            # handler did nothing. wait for state changes
            if self.eject_queue:
                self.debug_log("Waiting for ball changes or target_ready")
                wait_for_eject = self.machine.events.wait_for_event(
                    'balldevice_{}_ok_to_receive'.format(self.eject_queue[0][0].name))
            else:
                self.debug_log("Waiting for ball changes or eject_request")
                wait_for_eject = self._wait_for_eject_condition()
            wait_for_ball_changes = self._wait_for_ball_changes()
            wait_for_incoming_ball = self._incoming_ball_condition.wait()
            # TODO: wait for incoming balls timeout
            event = yield from Util.first([wait_for_ball_changes, wait_for_eject, wait_for_incoming_ball], self.machine.clock.loop)
            if hasattr(event, "_coro") and event._coro == wait_for_incoming_ball:
                # TODO: what happens when we support mechanical eject?
                # we got incoming ball without eject queue
                yield from self._waiting_for_ball()

            self.debug_log("Wait done")

    @asyncio.coroutine
    def _handle_ball_changes(self, balls):
        if self.balls < 0:
            raise AssertionError("Ball count went negative")

        if self.balls > balls:
            # balls went missing. we are idle
            missing_balls = self.balls - balls
            self.balls = balls
            yield from self._handle_missing_balls(balls=missing_balls)
            return True
        elif self.balls < balls:
            # unexpected balls
            unexpected_balls = balls - self.balls
            self.balls = balls
            yield from self._handle_new_balls(balls=unexpected_balls)

        # handle timeout incoming balls
        missing_balls = 0
        while (len(self._incoming_balls) and
                self._incoming_balls[0][0] <= self.machine.clock.get_time()):
            self._incoming_balls.popleft()
            self._handle_lost_incoming_ball()
            missing_balls += 1
        if missing_balls > 0:
            self.log.debug("Incoming ball expired!")
            yield from self._handle_missing_balls(balls=missing_balls)
            return True

        if self.get_additional_ball_capacity():
            # unblock blocked source_device_eject_attempts
            if not self.eject_queue or not self.balls:
                if self._blocked_eject_attempts:
                    (queue, source) = self._blocked_eject_attempts.popleft()
                    del source
                    queue.clear()
                    yield from self._waiting_for_ball()
                    return True

                yield from self._ok_to_receive()

        # No new balls
        # In idle those things can happen:
        # 1. A ball enter (via ball switches -> will call this method again)
        # 2. We get an eject request (via _eject_request)
        # 3. Sb wants to send us a ball (via _source_device_eject_attempt)

        # We might already have an eject queue. If yes go to eject
        return (yield from self._handle_eject_queue())

    @asyncio.coroutine
    def _handle_unexpected_balls(self, balls):
        self.log.debug("Received %s unexpected balls", balls)
        yield from self.machine.events.post_async('balldevice_captured_from_{}'.format(
            self.config['captures_from'].name),
            balls=balls)
        '''event: balldevice_captured_from_(device)

        desc: A ball device has just captured a ball from the device called
        (device)

        args:
        balls: The number of balls that were captured.

        '''

    @asyncio.coroutine
    def _handle_new_balls(self, balls):
        while len(self._incoming_balls) > 0 and balls > 0:
            balls -= 1
            self._incoming_balls.popleft()

        if balls > 0:
            yield from self._handle_unexpected_balls(balls)

        self.log.debug("Processing %s new balls", balls)
        result = yield from self.machine.events.post_relay_async('balldevice_{}_ball_enter'.format(
            self.name),
            new_balls=balls,
            unclaimed_balls=balls,
            device=self)
        '''event: balldevice_(name)_ball_enter

        desc: A ball (or balls) have just entered the ball device called
        "name".

        Note that this is a relay event based on the "unclaimed_balls" arg. Any
        unclaimed balls in the relay will be processed as new balls entering
        this device.

        args:

        unclaimed_balls: The number of balls that have not yet been claimed.
        device: A reference to the ball device object that is posting this
        event.


        '''
        self._balls_added_callback(result["new_balls"], result["unclaimed_balls"])

    @asyncio.coroutine
    def _handle_eject_queue(self):
        if self.eject_queue:
            self.debug_log("Handling eject queue")
            self.num_eject_attempts = 0
            if self.balls > 0:
                return (yield from self._check_eject_queue())
            else:
                yield from self._waiting_for_ball()
                return True

        return False

    @asyncio.coroutine
    def _check_eject_queue(self):
        if self.eject_queue:
            target = self.eject_queue[0][0]
            if target.get_additional_ball_capacity():
                yield from self._ejecting()
                return True

        return False

    # ------------------------ State: missing_balls ---------------------------
    @asyncio.coroutine
    def _handle_missing_balls(self, balls):
        if self.config['mechanical_eject']:
            # if the device supports mechanical eject we assume it was one
            self.mechanical_eject_in_progress = True
            # this is an unexpected eject. use default target
            self.eject_in_progress_target = self.config['eject_targets'][0]
            self.eject_in_progress_target.available_balls += 1
            yield from self._do_eject_attempt()
            return

        self._balls_missing(balls)

        return

    # ---------------------- State: waiting_for_ball --------------------------
    @asyncio.coroutine
    def _waiting_for_ball(self):
        self._state = "waiting_for_ball"
        self.debug_log("Waiting for ball to eject it")
        # This can happen
        # 1. ball counts can change (via _counted_balls)
        # 2. if mechanical_eject and the ball leaves source we go to
        #    waiting_for_ball_mechanical
        # 3. eject can fail at the source
        eject_failed = self._source_eject_failure_condition.wait()
        while True:
            balls = yield from self._count_balls()
            if self.balls > balls:
                # We dont have balls. How can that happen?
                raise AssertionError("We dont have balls but lose one!")
            elif self.balls < balls:
                # Return to idle state
                return

            ball_change = self._wait_for_ball_changes()
            futures = [ball_change, eject_failed]
            incoming_ball = None
            incoming_ball_timeout = None
            if self.config['mechanical_eject']:
                incoming_ball = self._incoming_ball_condition.wait()
                futures.append(incoming_ball)
            if self._incoming_balls:
                incoming_ball_timeout = asyncio.sleep(self._incoming_balls[0][0] - self.machine.clock.get_time(),
                                                      loop=self.machine.clock.loop)
                futures.append(incoming_ball_timeout)
            done, pending = yield from asyncio.wait(futures,
                                                    loop=self.machine.clock.loop,
                                                    return_when=asyncio.FIRST_COMPLETED)
            event = done.pop()
            if event._coro == eject_failed:
                self._cancel_eject()
                pending.pop().cancel()
                return

            # incoming ball expired handle that in idle
            if event._coro == incoming_ball_timeout:
                return

            if self.config['mechanical_eject'] and event._coro == incoming_ball:
                # TODO: this if can probably go
                if (yield from (self._waiting_for_ball_mechanical())):
                    return

    # ----------------- State: waiting_for_ball_mechanical --------------------
    @asyncio.coroutine
    def _waiting_for_ball_mechanical(self):
        # This can happen
        # 1. ball counts can change (via _counted_balls)
        # 2. eject can be confirmed
        # 2. eject of source can fail
        self.debug_log("Waiting for ball for mechanical eject")
        if len(self.eject_queue):
            self.eject_in_progress_target = self.eject_queue[0][0]
        else:
            self.eject_in_progress_target = self.config['eject_targets'][0]

        self.mechanical_eject_in_progress = True
        self.num_eject_attempts += 1
        self._notify_target_of_incoming_ball(self.eject_in_progress_target)
        yield from self._do_eject_attempt()

        while True:
            balls = yield from self._count_balls()
            if self.balls > balls:
                # We dont have balls. How can that happen?
                raise AssertionError("We dont have balls but lose one!")
            elif self.balls < balls:
                target = self.eject_in_progress_target
                self.eject_in_progress_target = None
                self._cancel_incoming_ball_at_target(target)
                self._cancel_eject_confirmation()
                self._inform_target_about_failed_confirm(target, 1, True)
                return True

            source_failure = self._source_eject_failure_condition.wait()
            source_failure_retry = self._source_eject_failure_retry_condition.wait()
            futures = [source_failure, source_failure_retry, self._wait_for_ball_changes()]
            event = yield from Util.first(futures, loop=self.machine.clock.loop)
            if event._coro == source_failure:
                self._cancel_eject_confirmation()
                self._cancel_incoming_ball_at_target(self.eject_in_progress_target)
                self._cancel_eject()
                return True
            elif event._coro == source_failure_retry:
                self._cancel_eject_confirmation()
                self._cancel_incoming_ball_at_target(self.eject_in_progress_target)
                # TODO: review why we do not cancel the eject here
                return False

    def add_incoming_ball(self, source):
        """Notify this device that there is a ball heading its way.

        Args:
            source: The source device this ball is coming from

        """
        timeout = 60
        self._incoming_balls.append((self.machine.clock.get_time() + timeout, source))
        self.delay.add(ms=timeout * 1000, callback=self._timeout_incoming)

        self._incoming_ball_condition.set()
        self._incoming_ball_condition.clear()

    def _timeout_incoming(self):
        # An incoming ball has not arrives in the time expected
        if len(self._incoming_balls) and self._state == "idle":
            return self._count_balls()

    def remove_incoming_ball(self, source):
        """Remove a ball from the incoming balls queue."""
        del source
        self._incoming_balls.popleft()

    # -------------------------- State: ball_left -----------------------------
    @asyncio.coroutine
    def _ball_left(self, timeout_time):
        self._state = "ball_left"
        self.debug_log("Ball left device")
        self.machine.events.remove_handler(self._trigger_eject_by_event)
        # TODO: handle entry switch here -> definitely new ball
        self.machine.events.post('balldevice_' + self.name + '_ball_left',
                                 balls=1,
                                 target=self.eject_in_progress_target,
                                 num_attempts=self.num_eject_attempts)
        '''event: balldevice_(name)_ball_left

        desc: A ball (or balls) just left the device (name).

        args:
            balls: The number of balls that just left
            target: The device the ball is heading to.
            num_attempts: The current count of how many eject attempts have
                been made.

        '''

        if self.config['confirm_eject_type'] == 'target':
            self._notify_target_of_incoming_ball(
                self.eject_in_progress_target)

        if self.eject_in_progress_target.is_playfield():
            if self.debug:
                self.log.debug("Target is playfield. Will confirm after "
                               "timeout if it did not return.")
            timeout = (
                self.config['eject_timeouts'][self.eject_in_progress_target]) + 500
            self.delay.add(name='playfield_confirmation',
                           ms=timeout,
                           callback=self.eject_success)

        try:
            yield from asyncio.wait_for(self._eject_success_condition.wait(), loop=self.machine.clock.loop,
                                        timeout=timeout_time - self.machine.clock.get_time())
        except asyncio.TimeoutError:
            yield from self._failed_confirm()
        else:
            self.eject_in_progress_target = None
            return

    # --------------------------- State: ejecting -----------------------------
    @asyncio.coroutine
    def _ejecting(self):
        self._state = "ejecting"
        self.debug_log("Ejecting ball")
        (self.eject_in_progress_target,
         self.mechanical_eject_in_progress,
         self.trigger_event) = (self.eject_queue.popleft())

        if self.debug:
            self.log.debug("Setting eject_in_progress_target: %s, " +
                           "mechanical: %s, trigger_events %s",
                           self.eject_in_progress_target.name,
                           self.mechanical_eject_in_progress,
                           self.trigger_event)

        self.num_eject_attempts += 1

        self.jam_switch_state_during_eject = (self.config['jam_switch'] and
                                              self.machine.switch_controller.is_active(
                                                  self.config['jam_switch'].name,
                                                  ms=self.config['entrance_count_delay']))

        if not self.trigger_event or self.mechanical_eject_in_progress:
            # no trigger_event -> just eject
            # mechanical eject -> will not eject. but be prepared
            yield from self._do_eject_attempt()

        if self.trigger_event:
            # TODO: what if ball is lost?
            # wait for trigger event
            yield from self.machine.events.wait_for_event("self.trigger_event")

        yield from self._wait_for_ball_left()

    def _trigger_eject_by_event(self, **kwargs):
        del kwargs
        self.machine.events.remove_handler(self._trigger_eject_by_event)

        if self.mechanical_eject_in_progress:
            self.mechanical_eject_in_progress = False
            self.ejector.eject_one_ball()
        else:
            # TODO: refactor this. its an hack for when balls return and we have to retry
            self._do_eject_attempt()

    @asyncio.coroutine
    def _do_eject_attempt(self):
        # Reachable from the following states:
        # ejecting
        # missing_balls
        # waiting_for_ball_mechanical

        yield from self.machine.events.post_queue_async(
            'balldevice_{}_ball_eject_attempt'.format(self.name),
            balls=1,
            target=self.eject_in_progress_target,
            source=self,
            mechanical_eject=(
                self.mechanical_eject_in_progress),
            num_attempts=self.num_eject_attempts)
        '''event: balldevice_(name)_ball_eject_attempt

        desc: The ball device called "name" is attempting to eject a ball (or
        balls). This is a queue event. The eject will not actually be attempted
        until the queue is cleared.

        args:

        balls: The number of balls that are to be ejected.
        taget: The target ball device that will receive these balls.
        source: The source device that will be ejecting the balls.
        mechanical_eject: Boolean as to whether this is a mechanical eject.
        num_attempts: How many eject attempts have been tried so far.
        '''
        yield from self._perform_eject(self.eject_in_progress_target)

    # --------------------------- State: failed_eject -------------------------
    @asyncio.coroutine
    def _failed_eject(self):
        self._state = "failed_eject"
        self.eject_failed()
        if self.config['max_eject_attempts'] != 0 and self.num_eject_attempts >= self.config['max_eject_attempts']:
            self._eject_permanently_failed()
            # What now? Ball is still in device or switch just broke. At least
            # we are unable to get rid of it
            return (yield from self._eject_broken())

        # ball did not leave. eject it again
        return (yield from self._ejecting())    # TODO: refactor this to a for loop

    # -------------------------- State: eject_broken --------------------------
    @asyncio.coroutine
    def _eject_broken(self):
        # The only way to get out of this state it to call reset on the device
        self._state = "eject_broken"
        self.log.warning(
            "Ball device is unable to eject ball. Stopping device")
        self.machine.events.post('balldevice_' + self.name +
                                 '_eject_broken', source=self)
        '''event: balldevice_(name)_eject_broken

        desc: The ball device called (name) is broken and cannot eject balls.

        '''

    # ------------------------ State: failed_confirm --------------------------
    @asyncio.coroutine
    def _failed_confirm(self):
        self._state = "failed_confirm"
        self.debug_log("Eject confirm failed")
        timeout = (self.config['ball_missing_timeouts']
                   [self.eject_in_progress_target])

        timeout_future = asyncio.sleep(timeout/1000, loop=self.machine.clock.loop)
        late_confirm_future = self._eject_success_condition.wait()

        while True:
            # count balls to see if the ball returns
            balls = yield from self._count_balls()

            if (self.config['jam_switch'] and
                    not self.jam_switch_state_during_eject and
                    self.machine.switch_controller.is_active(
                        self.config['jam_switch'].name,
                        ms=self.config['entrance_count_delay'])):
                # jam switch is active and was not active during eject.
                # assume failed eject!
                if self.config['confirm_eject_type'] == 'target':
                    self._cancel_incoming_ball_at_target(
                        self.eject_in_progress_target)
                self.balls += 1
                return (yield from self._failed_eject())

            if self.balls > balls:
                # we lost even more balls? if they do not come back until timeout
                # we will go to state "missing_balls" and forget about the first
                # one. Afterwards, we will go to state "idle" and it will handle
                # all additional missing balls
                pass
            elif self.balls < balls:
                # TODO: check if entry switch was active.
                # ball probably returned
                if self.config['confirm_eject_type'] == 'target':
                    self._cancel_incoming_ball_at_target(
                        self.eject_in_progress_target)
                self.balls += 1
                return (yield from self._failed_eject())

            done, pending = yield from asyncio.wait(iter([timeout_future, self._wait_for_ball_changes(),
                                                          late_confirm_future]),
                                                    loop=self.machine.clock.loop,
                                                    return_when=asyncio.FIRST_COMPLETED)

            event = done.pop()
            if event._coro == late_confirm_future:
                return
            elif event._coro == timeout_future:
                pending.pop().cancel()
                break

        self._ball_missing_timout()

    def _ball_missing_timout(self):
        if self._state != "failed_confirm":
            raise AssertionError("Invalid state " + self._state)

        if self.config['confirm_eject_type'] == 'target':
            self._cancel_incoming_ball_at_target(self.eject_in_progress_target)

        balls = 1
        # Handle lost ball
        self.log.debug("Lost %s balls during eject. Will ignore the "
                       "loss.", balls)
        self.eject_failed(retry=False)

        self._balls_missing(balls)

        # Reset target
        self.eject_in_progress_target = None


    def _source_device_balls_available(self, **kwargs):
        del kwargs
        if len(self.ball_requests):
            (target, player_controlled) = self.ball_requests.popleft()
            if self._setup_or_queue_eject_to_target(target, player_controlled):
                return False

    def _source_device_eject_attempt(self, balls, target, source, queue,
                                     **kwargs):
        del balls
        del kwargs

        if target != self:
            return

        if not self.is_ready_to_receive():
            # block the attempt until we are ready again
            self._blocked_eject_attempts.append((queue, source))
            queue.wait()
            return

    def _cancel_eject(self):
        target = self.eject_queue[0][0]
        self.eject_queue.popleft()
        # ripple this to the next device/register handler
        self.machine.events.post('balldevice_{}_ball_lost'.format(self.name),
                                 target=target)
        '''event: balldevice_(name)_ball_lost

        desc: A ball has been lost from the device (name), meaning the ball
            never made it to the target when this device attempted to eject
            it.

        args:
            target: The target device which was expecting to receive a ball
            from this device.

        '''

    def _source_device_eject_failed(self, balls, target, retry, **kwargs):
        del balls
        del kwargs

        if target != self:
            return

        if not retry:
            self._source_eject_failure_condition.set()
            self._source_eject_failure_condition.clear()
        else:
            self._source_eject_failure_retry_condition.set()
            self._source_eject_failure_retry_condition.clear()

        if self._state == "waiting_for_ball_mechanical":
            self._cancel_incoming_ball_at_target(self.eject_in_progress_target)
            self._cancel_eject_confirmation()
            if not retry:
                self._cancel_eject()
                return self._switch_state("idle")
            else:
                return self._switch_state("waiting_for_ball")


    def _source_device_ball_lost(self, target, **kwargs):
        del kwargs
        if target != self:
            return

        self._handle_lost_incoming_ball()

        if self._state == "waiting_for_ball":
            return self._switch_state("idle")

    def _handle_lost_incoming_ball(self):
        if self.debug:
            self.log.debug("Handling timeouts of incoming balls")
        if self.available_balls > 0:
            self.available_balls -= 1
            return

        if not len(self.eject_queue):
            raise AssertionError("Should have eject_queue")

        self._cancel_eject()

    # ---------------------- End of state handling code -----------------------

    def _parse_config(self):
        # ensure eject timeouts list matches the length of the eject targets
        if (len(self.config['eject_timeouts']) <
                len(self.config['eject_targets'])):
            self.config['eject_timeouts'] += ["10s"] * (
                len(self.config['eject_targets']) -
                len(self.config['eject_timeouts']))

        if (len(self.config['ball_missing_timeouts']) <
                len(self.config['eject_targets'])):
            self.config['ball_missing_timeouts'] += ["20s"] * (
                len(self.config['eject_targets']) -
                len(self.config['ball_missing_timeouts']))

        timeouts_list = self.config['eject_timeouts']
        self.config['eject_timeouts'] = dict()

        for i in range(len(self.config['eject_targets'])):
            self.config['eject_timeouts'][self.config['eject_targets'][i]] = (
                Util.string_to_ms(timeouts_list[i]))

        timeouts_list = self.config['ball_missing_timeouts']
        self.config['ball_missing_timeouts'] = dict()

        for i in range(len(self.config['eject_targets'])):
            self.config['ball_missing_timeouts'][
                self.config['eject_targets'][i]] = (
                Util.string_to_ms(timeouts_list[i]))
        # End code to create timeouts list ------------------------------------

        if self.config['ball_capacity'] is None:
            self.config['ball_capacity'] = len(self.config['ball_switches'])

    def _validate_config(self):
        # perform logical validation
        # a device cannot have hold_coil and eject_coil
        if (not self.config['eject_coil'] and not self.config['hold_coil'] and
                not self.config['mechanical_eject']):
            raise AssertionError('Configuration error in {} ball device. '
                                 'Device needs an eject_coil, a hold_coil, or '
                                 '"mechanical_eject: True"'.format(self.name))

        # entrance switch + mechanical eject is not supported
        if (len(self.config['ball_switches']) > 1 and
                self.config['mechanical_eject']):
            raise AssertionError('Configuration error in {} ball device. '
                                 'mechanical_eject can only be used with '
                                 'devices that have 1 ball switch'.
                                 format(self.name))

        # make sure timeouts are reasonable:
        # exit_count_delay < all eject_timeout
        if self.config['exit_count_delay'] > min(
                self.config['eject_timeouts'].values()):
            raise AssertionError('Configuration error in {} ball device. '
                                 'all eject_timeouts have to be larger than '
                                 'exit_count_delay'.
                                 format(self.name))

        # entrance_count_delay < all eject_timeout
        if self.config['entrance_count_delay'] > min(
                self.config['eject_timeouts'].values()):
            raise AssertionError('Configuration error in {} ball device. '
                                 'all eject_timeouts have to be larger than '
                                 'entrance_count_delay'.
                                 format(self.name))

        # all eject_timeout < all ball_missing_timeouts
        if max(self.config['eject_timeouts'].values()) > min(
                self.config['ball_missing_timeouts'].values()):
            raise AssertionError('Configuration error in {} ball device. '
                                 'all ball_missing_timeouts have to be larger '
                                 'than all eject_timeouts'.
                                 format(self.name))

        # all ball_missing_timeouts < incoming ball timeout
        if max(self.config['ball_missing_timeouts'].values()) > 60000:
            raise AssertionError('Configuration error in {} ball device. '
                                 'incoming ball timeout has to be larger '
                                 'than all ball_missing_timeouts'.
                                 format(self.name))

        if (self.config['confirm_eject_type'] == "switch" and
                not self.config['confirm_eject_switch']):
            raise AssertionError("When using confirm_eject_type switch you " +
                                 "to specify a confirm_eject_switch")

        if "drain" in self.tags and "trough" not in self.tags and not self.find_next_trough():
            raise AssertionError("No path to trough but device is tagged as drain")

        if ("drain" not in self.tags and "trough" not in self.tags and
                not self.find_path_to_target(self._target_on_unexpected_ball)):
            raise AssertionError("BallDevice {} has no path to target_on_unexpected_ball '{}'".format(
                self.name, self._target_on_unexpected_ball.name))

    def _register_handlers(self):
        # Register switch handlers with delays for entrance & exit counts
        for switch in self.config['ball_switches']:
            self.machine.switch_controller.add_switch_handler(
                switch_name=switch.name, state=1,
                ms=self.config['entrance_count_delay'],
                callback=self._switch_changed)
        for switch in self.config['ball_switches']:
            self.machine.switch_controller.add_switch_handler(
                switch_name=switch.name, state=0,
                ms=self.config['exit_count_delay'],
                callback=self._switch_changed)

        # Configure switch handlers for entrance switch activity
        if self.config['entrance_switch']:
            self.machine.switch_controller.add_switch_handler(
                switch_name=self.config['entrance_switch'].name, state=1,
                ms=0,
                callback=self._entrance_switch_handler)

            if self.config['entrance_switch_full_timeout'] and self.config['ball_capacity']:
                self.machine.switch_controller.add_switch_handler(
                    switch_name=self.config['entrance_switch'].name, state=1,
                    ms=self.config['entrance_switch_full_timeout'],
                    callback=self._entrance_switch_full_handler)

        # Configure event handlers to watch for target device status changes
        for target in self.config['eject_targets']:
            # Target device is now able to receive a ball
            self.machine.events.add_handler(
                'balldevice_{}_ok_to_receive'.format(target.name),
                self._target_ready, target=target)

    def load_config(self, config):
        """Load config."""
        super().load_config(config)

        # load targets and timeouts
        self._parse_config()

    def _initialize_phase_2(self):
        if self.config['target_on_unexpected_ball']:
            self._target_on_unexpected_ball = self.config['target_on_unexpected_ball']
        else:
            self._target_on_unexpected_ball = self.config['captures_from']

        # validate that configuration is valid
        self._validate_config()

        if self.config['eject_coil']:
            self.ejector = PulseCoilEjector(self)   # pylint: disable-msg=redefined-variable-type
        elif self.config['hold_coil']:
            self.ejector = HoldCoilEjector(self)    # pylint: disable-msg=redefined-variable-type

        # register switch and event handlers
        self._register_handlers()

    def _initialize_phase_3(self):
        if self.ejector:
            self.config['captures_from'].ball_search.register(
                self.config['ball_search_order'], self.ejector.ball_search)

        # Register events to watch for ejects targeted at this device
        for device in self.machine.ball_devices:
            if device.is_playfield():
                continue
            for target in device.config['eject_targets']:
                if target.name == self.name:
                    self._source_devices.append(device)
                    if self.debug:
                        self.log.debug("EVENT: %s to %s", device.name, target.name)

                    self.machine.events.add_handler(
                        'balldevice_{}_ball_eject_failed'.format(
                            device.name),
                        self._source_device_eject_failed)

                    self.machine.events.add_handler(
                        'balldevice_{}_ball_eject_attempt'.format(
                            device.name),
                        self._source_device_eject_attempt)

                    self.machine.events.add_handler(
                        'balldevice_{}_ball_lost'.format(device.name),
                        self._source_device_ball_lost)

                    self.machine.events.add_handler(
                        'balldevice_balls_available',
                        self._source_device_balls_available)

                    break

    def _initialize_phase_4(self, **kwargs):
        del kwargs
        self.runner = self.machine.clock.loop.create_task(self._run())
        self.runner.add_done_callback(self._done)

    @staticmethod
    def _done(future):
        """Evaluate result of task.

        Will raise exceptions from within task.
        """
        try:
            future.result()
        except asyncio.CancelledError:
            pass

    def get_status(self, request=None):  # pragma: no cover
        """Return a dictionary of current status of this ball device.

        Args:
            request: A string of what status item you'd like to request.
                Default will return all status items.
                Options include:
                * balls
                * eject_in_progress_target
                * eject_queue

        Returns:
            A dictionary with the following keys:
                * balls
                * eject_in_progress_target
                * eject_queue
        """
        if request == 'balls':
            return self.balls
        elif request == 'eject_in_progress_target':
            return self.eject_in_progress_target
        elif request == 'eject_queue':
            return self.eject_queue,
        else:
            return {'balls': self.balls,
                    'eject_in_progress_target': self.eject_in_progress_target,
                    'eject_queue': self.eject_queue,
                    }

    def status_dump(self):  # pragma: no cover
        """Dump the full current status of the ball device to the log."""
        if self.debug:
            self.log.debug("+-----------------------------------------+")
            self.log.debug("| balls: {}".format(
                self.balls).ljust(42) + "|")
            self.log.debug("| eject_in_progress_target: {}".format(
                self.eject_in_progress_target).ljust(42) + "|")
            self.log.debug("| num_eject_attempts: {}".format(
                self.num_eject_attempts).ljust(42) + "|")
            self.log.debug("| eject queue: {}".format(
                self.eject_queue).ljust(42) + "|")
            self.log.debug("| mechanical_eject_in_progress: {}".format(
                self.mechanical_eject_in_progress).ljust(42) + "|")
            self.log.debug("+-----------------------------------------+")

    def _switch_changed(self, **kwargs):
        del kwargs
        self._ball_counter_condition.set()
        self._ball_counter_condition.clear()

    def _wait_for_ball_changes(self):
        return self._ball_counter_condition.wait()

    @asyncio.coroutine
    def _count_balls(self, **kwargs):
        del kwargs

        if not self.config['ball_switches']:
            return self.balls

        while True:
            if self.debug:
                self.log.debug("Counting balls by checking switches")
            try:
                return self._count_ball_switches()
            except ValueError:
                yield from self._ball_counter_condition.wait()

    def _count_ball_switches(self):
        # Count only. Do not change any state here!
        ball_count = 0

        for switch in self.config['ball_switches']:
            valid = False
            if self.machine.switch_controller.is_active(switch.name,
                                                        ms=self.config[
                                                            'entrance_count_delay']):
                ball_count += 1
                valid = True
                if self.debug:
                    self.log.debug("Confirmed active switch: %s", switch.name)
            elif self.machine.switch_controller.is_inactive(switch.name,
                                                            ms=self.config[
                                                                'exit_count_delay']):
                if self.debug:
                    self.log.debug("Confirmed inactive switch: %s",
                                   switch.name)
                valid = True

            if not valid:  # one of our switches wasn't valid long enough
                # recount will happen automatically after the time passes
                # via the switch handler for count
                if self.debug:
                    self.log.debug("Switch '%s' changed too recently. "
                                   "Aborting count & returning previous "
                                   "count value", switch.name)
                raise ValueError('Count not stable yet. Run again!')

        if self.debug:
            self.log.debug("Counted %s balls", ball_count)

        return ball_count

    def _balls_added_callback(self, new_balls, unclaimed_balls, **kwargs):
        del kwargs
        # If we still have unclaimed_balls here, that means that no one claimed
        # them, so essentially they're "stuck." So we just eject them unless
        # this device is tagged 'trough' in which case we let it keep them.

        self.available_balls += new_balls

        if unclaimed_balls:
            if 'trough' in self.tags:
                # ball already reached trough. everything is fine
                pass
            elif 'drain' in self.tags:
                # try to eject to next trough
                trough = self.find_next_trough()

                if not trough:
                    raise AssertionError("Could not find path to trough")

                for dummy_iterator in range(unclaimed_balls):
                    self._setup_or_queue_eject_to_target(trough)
            else:
                target = self._target_on_unexpected_ball

                # try to eject to configured target
                path = self.find_path_to_target(target)

                if not path:
                    raise AssertionError("Could not find path to playfield {}".format(target.name))

                if self.debug:
                    self.log.debug("Ejecting %s unexpected balls using path %s", unclaimed_balls, path)

                for dummy_iterator in range(unclaimed_balls):
                    self.setup_eject_chain(path, not self.config['auto_fire_on_unexpected_ball'])

        # we might have ball requests locally. serve them first
        if self.ball_requests:
            self._source_device_balls_available()

        # tell targets that we have balls available
        for dummy_iterator in range(new_balls):
            self.machine.events.post_boolean('balldevice_balls_available')

    def _balls_missing(self, balls):
        # Called when ball_count finds that balls are missing from this device
        if self.debug:
            self.log.debug("%s ball(s) missing from device. Mechanical eject?"
                           " %s", abs(balls),
                           self.mechanical_eject_in_progress)

        self.machine.events.post('balldevice_{}_ball_missing'.format(
            abs(balls)))
        '''event: balldevice_(balls)_ball_missing.
        desc: The number of (balls) is missing. Note this event is
        posted in addition to the generic *balldevice_ball_missing* event.
        '''
        self.machine.events.post('balldevice_ball_missing',
                                 balls=abs(balls))
        '''event: balldevice_ball_missing
        desc: A ball is missing from a device.
        args:
            balls: The number of balls that are missing
        '''

        # add ball to default target
        self.config['ball_missing_target'].add_missing_balls(balls)

    def is_full(self):
        """Check to see if this device is full.

        Full meaning it is holding either the max number of balls it can hold, or it's holding all the known
        balls in the machine.

        Returns: True or False
        """
        if self.config['ball_capacity'] and self.balls >= self.config['ball_capacity']:
            return True
        elif self.balls >= self.machine.ball_controller.num_balls_known:
            return True
        else:
            return False

    def entrance(self, **kwargs):
        """Event handler for entrance events."""
        del kwargs
        self._entrance_switch_handler()

    def _entrance_switch_full_handler(self):
        # a ball is sitting on the entrance_switch. assume the device is full
        new_balls = self.config['ball_capacity'] - self.balls
        if new_balls > 0:
            self.log.debug("Ball is sitting on entrance_switch. Assuming "
                           "device is full. Adding %s balls and setting balls"
                           "to %s", new_balls, self.config['ball_capacity'])
            self.balls += new_balls
            self._handle_new_balls(new_balls)
            self.machine.ball_controller.trigger_ball_count()

    def _entrance_switch_handler(self):
        # A ball has triggered this device's entrance switch

        if not self.config['ball_switches']:
            if self.is_full():
                self.log.warning("Device received balls but is already full. "
                                 "Ignoring!")
                # TODO: ball should be added to pf instead
                return

            self.balls += 1
            self._handle_new_balls(1)
            self.machine.ball_controller.trigger_ball_count()

    @property
    def state(self):
        """Return the device state."""
        return self._state

    def is_ball_count_stable(self):
        """Return if ball count is stable."""
        return self._state == "idle" and self._idle_counted and not len(self._incoming_balls)

    def is_ready_to_receive(self):
        """Return if device is ready to receive a ball."""
        return ((self._state == "idle" and self._idle_counted) or
                (self._state == "waiting_for_ball") and
                self.balls < self.config['ball_capacity'])

    def get_real_additional_capacity(self):
        """Return how many more balls this device can hold."""
        if self.config['ball_capacity'] - self.balls < 0:
            self.log.warning("Device reporting more balls contained than its "
                             "capacity.")

        return self.config['ball_capacity'] - self.balls

    def get_additional_ball_capacity(self):
        """Return an integer value of the number of balls this device can receive.

        A return value of 0 means that this device is full and/or
        that it's not able to receive any balls at this time due to a
        current eject_in_progress. This methods also accounts for incoming balls which means that there may be more
        space in the device then this method returns.
        """
        capacity = self.get_real_additional_capacity()
        capacity -= len(self._incoming_balls)
        if self.eject_in_progress_target:
            capacity -= 1
        if capacity < 0:
            return 0
        else:
            return capacity

    def find_one_available_ball(self, path=deque()):
        """Find a path to a source device which has at least one available ball."""
        # copy path
        path = deque(path)

        # prevent loops
        if self in path:
            return False

        path.appendleft(self)

        if self.available_balls > 0 and len(path) > 1:
            return path

        for source in self._source_devices:
            full_path = source.find_one_available_ball(path=path)
            if full_path:
                return full_path

        return False

    def request_ball(self, balls=1, **kwargs):
        """Request that one or more balls is added to this device.

        Args:
            balls: Integer of the number of balls that should be added to this
                device. A value of -1 will cause this device to try to fill
                itself.
            **kwargs: unused
        """
        del kwargs
        if self.debug:
            self.log.debug("Requesting Ball(s). Balls=%s", balls)

        for dummy_iterator in range(balls):
            self._setup_or_queue_eject_to_target(self)

        return balls

    def _setup_or_queue_eject_to_target(self, target, player_controlled=False):
        path_to_target = self.find_path_to_target(target)
        if self.available_balls > 0 and self != target:
            path = path_to_target
        else:

            path = self.find_one_available_ball()
            if not path:
                # put into queue here
                self.ball_requests.append((target, player_controlled))
                return False

            if target != self:
                if target not in self.config['eject_targets']:
                    raise AssertionError(
                        "Do not know how to eject to " + target.name)

                path_to_target.popleft()    # remove self from path
                path.extend(path_to_target)

        path[0].setup_eject_chain(path, player_controlled)

        return True

    def setup_player_controlled_eject(self, balls=1, target=None):
        """Setup a player controlled eject."""
        if self.debug:
            self.log.debug("Setting up player-controlled eject. Balls: %s, "
                           "Target: %s, player_controlled_eject_event: %s",
                           balls, target,
                           self.config['player_controlled_eject_event'])

        assert balls == 1

        if self.config['mechanical_eject'] or (
                self.config['player_controlled_eject_event'] and self.ejector):

            self._setup_or_queue_eject_to_target(target, True)

            return self._count_balls()

        else:
            self.eject(balls, target=target)

    def setup_eject_chain(self, path, player_controlled=False):
        """Setup an eject chain."""
        path = deque(path)
        if self.available_balls <= 0:
            raise AssertionError("Tried to setup an eject chain, but there are"
                                 " no available balls. Device: {}, Path: {}"
                                 .format(self.name, path))

        self.available_balls -= 1

        target = path[len(path) - 1]
        source = path.popleft()
        if source != self:
            raise AssertionError("Path starts somewhere else!")

        self.setup_eject_chain_next_hop(path, player_controlled)

        target.available_balls += 1

        self.machine.events.post_boolean('balldevice_balls_available')
        '''event: balldevice_balls_available
        desc: A device has balls available to be ejected.
        '''

    def setup_eject_chain_next_hop(self, path, player_controlled):
        """Setup one hop of the eject chain."""
        next_hop = path.popleft()

        if next_hop not in self.config['eject_targets']:
            raise AssertionError("Broken path")

        # append to queue
        if player_controlled and (self.config['mechanical_eject'] or self.config['player_controlled_eject_event']):
            self.eject_queue.append((next_hop, self.config['mechanical_eject'],
                                     self.config[
                                         'player_controlled_eject_event']))
        else:
            self.eject_queue.append((next_hop, False, None))

        # check if we traversed the whole path
        if len(path) > 0:
            next_hop.setup_eject_chain_next_hop(path, player_controlled)

        self._eject_request_condition.set()
        self._eject_request_condition.clear()

    def _wait_for_eject_condition(self):
        return self._eject_request_condition.wait()

    def find_next_trough(self):
        """Find next trough after device."""
        # are we a trough?
        if 'trough' in self.tags:
            return self

        # otherwise find any target which can
        for target_device in self.config['eject_targets']:
            if target_device.is_playfield():
                continue
            trough = target_device.find_next_trough()
            if trough:
                return trough

        return False

    def find_path_to_target(self, target):
        """Find a path to this target."""
        # if we can eject to target directly just do it
        if target in self.config['eject_targets']:
            path = deque()
            path.appendleft(target)
            path.appendleft(self)
            return path
        else:
            # otherwise find any target which can
            for target_device in self.config['eject_targets']:
                if target_device.is_playfield():
                    continue
                path = target_device.find_path_to_target(target)
                if path:
                    path.appendleft(self)
                    return path

        return False

    def eject(self, balls=1, target=None, **kwargs):
        """Eject ball to target."""
        del kwargs
        if not target:
            target = self._target_on_unexpected_ball

        if self.debug:
            self.log.debug('Adding %s ball(s) to the eject_queue with target %s.',
                           balls, target)

        # add request to queue
        for dummy_iterator in range(balls):
            self._setup_or_queue_eject_to_target(target)

        if self.debug:
            self.log.debug('Queue %s.', self.eject_queue)

    def eject_all(self, target=None, **kwargs):
        """Eject all the balls from this device.

        Args:
            target: The string or BallDevice target for this eject. Default of
                None means `playfield`.
            **kwargs: unused

        Returns:
            True if there are balls to eject. False if this device is empty.
        """
        del kwargs
        if self.debug:
            self.log.debug("Ejecting all balls")
        if self.available_balls > 0:
            self.eject(balls=self.available_balls, target=target)
            return True
        else:
            return False

    def _eject_status(self, dt):
        del dt
        if self.debug:

            try:
                self.log.debug("DEBUG: Eject duration: %ss. Target: %s",
                               round(self.machine.clock.get_time() - self.eject_start_time,
                                     2),
                               self.eject_in_progress_target.name)
            except AttributeError:
                self.log.debug("DEBUG: Eject duration: %ss. Target: None",
                               round(self.machine.clock.get_time() - self.eject_start_time,
                                     2))

    def _ball_left_device(self, **kwargs):
        del kwargs
        if self._state != "ejecting":
            raise AssertionError("Device in wrong state {}. Should be in ejecting.".format(self._state))
        # remove handler
        for switch in self.config['ball_switches']:
            self.machine.switch_controller.remove_switch_handler(
                switch_name=switch.name,
                callback=self._ball_left_device,
                state=0)
        self.balls -= 1

        self.log.debug("Ball left. New count %s", self.balls)
        return self._switch_state("ball_left")

    @asyncio.coroutine
    def _perform_eject(self, target, **kwargs):
        del kwargs
        self._setup_eject_confirmation(target)
        self.log.debug("Ejecting ball to %s", target.name)
        yield from self.machine.events.post_async(
            'balldevice_{}_ejecting_ball'.format(self.name),
            balls=1,
            target=self.eject_in_progress_target,
            source=self,
            mechanical_eject=self.mechanical_eject_in_progress,
            num_attempts=self.num_eject_attempts)
        '''event: balldevice_(name)_ejecting_ball

        desc: The ball device called "name" is ejecting a ball right now.

        args:

        balls: The number of balls that are to be ejected.
        taget: The target ball device that will receive these balls.
        source: The source device that will be ejecting the balls.
        mechanical_eject: Boolean as to whether this is a mechanical eject.
        num_attempts: How many eject attempts have been tried so far.
        '''

    @asyncio.coroutine
    def _wait_for_ball_left(self):
        timeout = self.config['eject_timeouts'][self.eject_in_progress_target] / 1000
        timeout_time = self.machine.clock.get_time() + timeout

        switch_waiters = []
        if self.config['ball_switches']:
            # wait until one of the active switches turns off
            for switch in self.config['ball_switches']:
                # only consider active switches
                if self.machine.switch_controller.is_active(switch.name):
                    switch_waiters.append(self.machine.switch_controller.wait_for_switch(
                        switch_name=switch.name,
                        state=0))

        if self.ejector:
            if self.mechanical_eject_in_progress:
                self.log.debug("Will not fire eject coil because of mechanical"
                               "eject")
            else:
                self.ejector.eject_one_ball()

        # TODO: put some minimal wait here for cases without switches

        # only wait if we have switches to wait on
        if switch_waiters:
            try:
                yield from Util.first(switch_waiters, self.machine.clock.loop, timeout=timeout)
            except asyncio.TimeoutError:
                yield from self._failed_eject()
                return

        # remove the ball from our count
        self.balls -= 1
        yield from self._ball_left(timeout_time)

    def hold(self, **kwargs):
        """Event handler for hold event."""
        del kwargs
        # TODO: remove when migrating config to ejectors
        self.ejector.hold()

    def _playfield_active(self, playfield, **kwargs):
        del playfield
        del kwargs
        self.eject_success()
        return False

    def _setup_eject_confirmation_to_playfield(self, target, timeout):
        if self.debug:
            self.log.debug("Target is a playfield. Will confirm eject " +
                           "when a %s switch is hit", target.name)

        self.machine.events.add_handler(
            '{}_active'.format(target.name),
            self._playfield_active, playfield=target)

        if self.mechanical_eject_in_progress and self._state == "waiting_for_ball_mechanical":
            if self.debug:
                self.log.debug("Target is playfield. Will confirm after "
                               "timeout if it did not return.")
            timeout_combined = timeout + self._incoming_balls[0][1].config['eject_timeouts'][self]

            if timeout == timeout_combined:
                timeout_combined += 500

            self.delay.add(name='playfield_confirmation',
                           ms=timeout_combined,
                           callback=self.eject_success)

    def _setup_eject_confirmation_to_target(self, target, timeout):
        if not target:
            raise AssertionError("we got an eject confirmation request "
                                 "with no target. This shouldn't happen. "
                                 "Post to the forum if you see this.")

        if self.debug:
            self.log.debug("Will confirm eject via ball entry into '%s' "
                           "with a confirmation timeout of %sms",
                           target.name, timeout)

        # ball_enter does mean sth different for the playfield.
        if not target.is_playfield():
            # watch for ball entry event on the target device
            self.machine.events.add_handler(
                'balldevice_' + target.name +
                '_ball_enter', self.eject_success, priority=100000)

    def _setup_eject_confirmation_via_switch(self):
        if self.debug:
            self.log.debug("Will confirm eject via activation of switch "
                           "'%s'",
                           self.config['confirm_eject_switch'].name)
        # watch for that switch to activate momentarily
        # for more complex scenarios use logic_block + event confirmation
        self.machine.switch_controller.add_switch_handler(
            switch_name=self.config['confirm_eject_switch'].name,
            callback=self.eject_success,
            state=1, ms=0)

    def _setup_eject_confirmation_via_event(self):
        if self.debug:
            self.log.debug("Will confirm eject via posting of event '%s'",
                           self.config['confirm_eject_event'])
        # watch for that event
        self.machine.events.add_handler(
            self.config['confirm_eject_event'], self.eject_success)

    def _setup_eject_confirmation_fake(self):
        # for devices without ball_switches and entry_switch
        # we use delay to keep the call order
        if self.config['ball_switches']:
            raise AssertionError("Cannot use fake with ball switches")

        self.delay.add(name='target_eject_confirmation_timeout',
                       ms=1, callback=self.eject_success)

    def _setup_eject_confirmation(self, target):
        # Called after an eject request to confirm the eject. The exact method
        # of confirmation depends on how this ball device has been configured
        # and what target it's ejecting to

        # args are target device

        if self.debug:
            self.log.debug("Setting up eject confirmation")
            self.eject_start_time = self.machine.clock.get_time()
            self.log.debug("Eject start time: %s", self.eject_start_time)
            self._eject_status_logger = self.machine.clock.schedule_interval(self._eject_status, 1)

        timeout = self.config['eject_timeouts'][target]
        # if timeout:
        #     # set up the delay to check for the failed the eject
        #     self.delay.add(name='target_eject_confirmation_timeout',
        #                    ms=timeout,
        #                    callback=self._eject_timeout)

        if target and target.is_playfield():
            self._setup_eject_confirmation_to_playfield(target, timeout)

        if self.config['confirm_eject_type'] == 'target':
            self._setup_eject_confirmation_to_target(target, timeout)

        elif self.config['confirm_eject_type'] == 'switch':
            self._setup_eject_confirmation_via_switch()

        elif self.config['confirm_eject_type'] == 'event':
            self._setup_eject_confirmation_via_event()

        elif self.config['confirm_eject_type'] == 'fake':
            self._setup_eject_confirmation_fake()

        else:
            raise AssertionError("Invalid confirm_eject_type setting: " +
                                 self.config['confirm_eject_type'])

    def _cancel_eject_confirmation(self):
        if self.debug:
            self.log.debug("Canceling eject confirmations")
            if self._eject_status_logger:
                self.machine.clock.unschedule(self._eject_status_logger)
                self._eject_status_logger = None
        self.eject_in_progress_target = None

        # Remove any event watching for success
        self.machine.events.remove_handler(self.eject_success)
        self.machine.events.remove_handler(self._playfield_active)
        self.machine.events.remove_handler(self._trigger_eject_by_event)

        self.mechanical_eject_in_progress = False

        # remove handler for ball left device
        for switch in self.config['ball_switches']:
            self.machine.switch_controller.remove_switch_handler(
                switch_name=switch.name,
                callback=self._ball_left_device,
                state=0)
            self.machine.switch_controller.remove_switch_handler(
                switch_name=switch.name,
                callback=self.eject_success,
                ms=self.config['exit_count_delay'],
                state=0)

        # Remove any switch handlers
        if self.config['confirm_eject_type'] == 'switch':
            self.machine.switch_controller.remove_switch_handler(
                switch_name=self.config['confirm_eject_switch'].name,
                callback=self.eject_success,
                state=1, ms=0)

        # Remove any delays that were watching for failures
        self.delay.remove('target_eject_confirmation_timeout')
        self.delay.remove('ball_missing_timeout')
        self.delay.remove('playfield_confirmation')

    def _notify_target_of_incoming_ball(self, target):
        target.add_incoming_ball(self)

    def _cancel_incoming_ball_at_target(self, target):
        target.remove_incoming_ball(self)

    def eject_success(self, **kwargs):
        """We got an eject success for this device."""
        del kwargs

        if self._state == "ejecting":
            self.log.debug("Got an eject_success before the switch changed "
                           "state in the device. Ignoring!")
            return

        if self._state == "waiting_for_ball_mechanical":
            # confirm eject of our source device
            self._incoming_balls[0][1].eject_success()
            # remove eject from queue if we have one
            if len(self.eject_queue):
                self.eject_queue.popleft()
            else:
                # because the path was not set up. just add the ball
                self.eject_in_progress_target.available_balls += 1
            self._incoming_balls.popleft()
        elif self._state != "ball_left" and self._state != "failed_confirm":
            raise AssertionError(
                "Got an eject_success in wrong state " + self._state)
        elif self.config['confirm_eject_type'] != 'target':
            # notify if not in waiting_for_ball_mechanical
            self._notify_target_of_incoming_ball(self.eject_in_progress_target)

        if self.debug:
            self.log.debug("In eject_success(). Eject target: %s",
                           self.eject_in_progress_target)

        if self.debug:
            self.log.debug("Eject duration: %ss",
                           self.machine.clock.get_time() - self.eject_start_time)

        if self.debug:
            self.log.debug("Confirmed successful eject")

        # Create a temp attribute here so the real one is None when the
        # event is posted.
        eject_target = self.eject_in_progress_target
        self.num_eject_attempts = 0
        self.eject_in_progress_target = None
        balls_ejected = 1

        self._cancel_eject_confirmation()

        self._eject_success_condition.set()
        self._eject_success_condition.clear()

        self.machine.events.post('balldevice_' + self.name +
                                 '_ball_eject_success',
                                 balls=balls_ejected,
                                 target=eject_target)
        '''event: balldevice_(name)_ball_eject_success
        desc: One or more balls has successfully ejected from the device
            (name).
        args:
            balls: The number of balls that have successfully ejected.
            target: The target device that has received (or will be receiving)
                the ejected ball(s).
        '''

    def _eject_timeout(self):
        if self.debug:
            self.log.debug("Got eject timeout")

        if self._state == "ball_left":
            return self._switch_state("failed_confirm")
        elif self._state == "ejecting":
            if not self.mechanical_eject_in_progress:
                return self._switch_state("failed_eject")
        elif self._state == "waiting_for_ball_mechanical":
            return
        else:
            raise AssertionError("Invalid state " + self._state)

    def eject_failed(self, retry=True):
        """Mark the current eject in progress as 'failed'.

        Note this is not typically a method that would be called manually. It's
        called automatically based on ejects timing out or balls falling back
        into devices while they're in the process of ejecting. But you can call
        it manually if you want to if you have some other way of knowing that
        the eject failed that the core can't figure out on it's own.

        Args:
            retry: Boolean as to whether this eject should be retried. If True,
                the ball device will retry the eject again as long as the
                'max_eject_attempts' has not been exceeded. Default is True.

        """
        # Put the current target back in the queue so we can try again
        # This sets up the timeout back to the default. Wonder if we should
        # add some intelligence to make this longer or shorter?

        if self.debug:
            self.log.debug("Eject failed")

        if retry:
            self.eject_queue.appendleft((self.eject_in_progress_target,
                                         self.mechanical_eject_in_progress,
                                         self.trigger_event))

        # Remember variables for event
        target = self.eject_in_progress_target
        balls = 1

        # Reset the stuff that showed a current eject in progress
        self.eject_in_progress_target = None

        if self.debug:
            self.log.debug("Eject duration: %ss",
                           self.machine.clock.get_time() - self.eject_start_time)

        # cancel eject confirmations
        self._cancel_eject_confirmation()
        self._inform_target_about_failed_confirm(target, balls, retry)

    def _inform_target_about_failed_confirm(self, target, balls, retry):
        self.machine.events.post('balldevice_' + self.name +
                                 '_ball_eject_failed',
                                 target=target,
                                 balls=balls,
                                 retry=retry,
                                 num_attempts=self.num_eject_attempts)
        '''event: balldevice_(name)_ball_eject_failed
        desc: A ball (or balls) has failed to eject from the device (name).
        args:
            target: The target device that was supposed to receive the ejected
                balls.
            balls: The number of balls that failed to eject.
            retry: Boolean as to whether this eject will be retried.
            num_attempts: How many attemps have been made to eject this ball
                (or balls).
        '''

    def _eject_permanently_failed(self):
        self.log.warning("Eject failed %s times. Permanently giving up.",
                         self.config['max_eject_attempts'])
        self.machine.events.post('balldevice_' + self.name +
                                 '_ball_eject_permanent_failure')
        '''event: balldevice_(name)_ball_eject_permanent_failure
        desc: The device (name) failed to eject a ball and the number of
            retries has been met, so it will not try to eject further.
        '''

    @asyncio.coroutine
    def _ok_to_receive(self):
        # Post an event announcing that it's ok for this device to receive a
        # ball
        yield from self.machine.events.post_async(
            'balldevice_{}_ok_to_receive'.format(self.name),
            balls=self.get_additional_ball_capacity())
        '''event: balldevice_(name)_ok_to_receive
        desc: The ball device (name) now has capicity to receive a ball (or
            balls). This event is posted after a device that was full has
            successfully ejected and is now able to receive balls.
        args:
            balls: The number of balls this device can now receive.
        '''

    @classmethod
    def is_playfield(cls):
        """Return True if this ball device is a Playfield-type device, False if it's a regular ball device."""
        return False
