"""Unit tests for the events module."""

from collections import namedtuple
from datetime import datetime
from io import BytesIO
import logging
import os
import sys
import time

import pytest

from pydicom.dataset import Dataset
from pydicom.tag import BaseTag
from pydicom.uid import ImplicitVRLittleEndian

from pynetdicom import (
    AE, evt, _config, Association, debug_logger, build_context
)
from pynetdicom.events import (
    Event, trigger, _async_ops_handler, _sop_common_handler,
    _sop_extended_handler, _user_identity_handler, _c_echo_handler,
    _c_get_handler, _c_find_handler, _c_move_handler, _c_store_handler,
    _n_action_handler, _n_create_handler, _n_delete_handler,
    _n_event_report_handler, _n_get_handler, _n_set_handler
)
from pynetdicom.dimse_messages import (
    N_ACTION, N_CREATE, N_EVENT_REPORT, N_SET, N_GET, N_DELETE
)
from pynetdicom.sop_class import VerificationSOPClass


#debug_logger()


def test_intervention_namedtuple():
    """Test the InterventionEvent namedtuple."""
    event = evt.InterventionEvent('some name', 'some description')
    assert event.name == 'some name'
    assert event.description == 'some description'
    assert event.is_intervention is True
    assert event.is_notification is False

def test_notification_namedtuple():
    """Test the NotificationEvent namedtuple."""
    event = evt.NotificationEvent('some name', 'some description')
    assert event.name == 'some name'
    assert event.description == 'some description'
    assert event.is_intervention is False
    assert event.is_notification is True

def test_intervention_global():
    """Test the _INTERVENTION_EVENTS global."""
    assert evt.EVT_C_ECHO in evt._INTERVENTION_EVENTS
    assert evt.EVT_DATA_RECV not in evt._INTERVENTION_EVENTS

def test_notification_global():
    """Test the _NOTIFICATION_EVENTS global."""
    assert evt.EVT_C_ECHO not in evt._NOTIFICATION_EVENTS
    assert evt.EVT_DATA_RECV in evt._NOTIFICATION_EVENTS


class TestEvent(object):
    """Tests for event.Event."""
    def setup(self):
        self.ae = None
        _config.LOG_HANDLER_LEVEL = 'none'

        # Implicit VR Little Endian
        self.bytestream = BytesIO(
            #  (0010,0010) PatientName
            # | tag           | length        | value
            b'\x10\x00\x10\x00\x09\x00\x00\x00'
            b'\x54\x45\x53\x54\x5E\x54\x65\x73\x74'
        )
        self.context = build_context('1.2.840.10008.1.1',
                                     ImplicitVRLittleEndian)

    def teardown(self):
        if self.ae:
            self.ae.shutdown()

        _config.LOG_HANDLER_LEVEL = 'standard'

    def test_init(self):
        """Test initialisation of event.Event."""
        event = evt.Event(None, evt.EVT_C_STORE)
        assert event.assoc is None
        assert event._event == evt.EVT_C_STORE
        assert isinstance(event.timestamp, datetime)
        assert event.event == evt.EVT_C_STORE
        assert event.event.name == 'EVT_C_STORE'
        assert isinstance(event.event.description, str)

        def callable():
            return 'some value'

        event = evt.Event(
            None, evt.EVT_C_STORE, {'aa' : True, 'bb' : False, 'cc' : callable}
        )
        assert event.assoc is None
        assert event._event == evt.EVT_C_STORE
        assert event.event == evt.EVT_C_STORE
        assert isinstance(event.timestamp, datetime)
        assert event.event.name == 'EVT_C_STORE'
        assert isinstance(event.event.description, str)
        assert event.cc() == 'some value'
        assert event.aa is True
        assert event.bb is False

    def test_raises(self):
        """Test property getters raise if not correct event type."""
        event = evt.Event(None, evt.EVT_DATA_RECV)
        msg = (
            r"The corresponding event is not a C-STORE "
            r"request and has no 'Data Set' parameter"
        )
        with pytest.raises(AttributeError, match=msg):
            event.dataset

        msg = (
            r"The corresponding event is not a C-FIND, C-GET or C-MOVE request "
            r"and has no 'Identifier' parameter"
        )
        with pytest.raises(AttributeError, match=msg):
            event.identifier

        msg = (
            r"The corresponding event is not a C-MOVE request "
            r"and has no 'Move Destination' parameter"
        )
        with pytest.raises(AttributeError, match=msg):
            event.move_destination

        msg = (
            r"The corresponding event is not an N-ACTION request and has no "
            r"'Action Information' parameter"
        )
        with pytest.raises(AttributeError, match=msg):
            event.action_information

        msg = (
            r"The corresponding event is not an N-CREATE request and has no "
            r"'Attribute List' parameter"
        )
        with pytest.raises(AttributeError, match=msg):
            event.attribute_list

        msg = (
            r"The corresponding event is not an N-EVENT-REPORT request and "
            r"has no 'Event Information' parameter"
        )
        with pytest.raises(AttributeError, match=msg):
            event.event_information

        msg = (
            r"The corresponding event is not an N-SET request and has no "
            r"'Modification List' parameter"
        )
        with pytest.raises(AttributeError, match=msg):
            event.modification_list

        msg = (
            r"The corresponding event is not an N-GET request and has no "
            r"'Attribute Identifier List' parameter"
        )
        with pytest.raises(AttributeError, match=msg):
            event.attribute_identifiers

    def test_is_cancelled_non(self):
        """Test Event.is_cancelled with wrong event type."""
        event = evt.Event(None, evt.EVT_DATA_RECV)
        assert event.is_cancelled is False

        def is_cancelled(msg_id):
            if msg_id in [1, 2, 3]:
                return True

            return False

        # No Message ID
        event = evt.Event(
            None, evt.EVT_DATA_RECV, {'_is_cancelled' : is_cancelled}
        )
        assert event.is_cancelled is False

    def test_is_cancelled(self):
        """Test Event.is_cancelled with correct event type."""
        def is_cancelled(msg_id):
            if msg_id in [1, 2, 3]:
                return True

            return False

        message = namedtuple('Message', ['MessageID'])
        msg = message(1)
        msg2 = message(7)

        event = evt.Event(
            None,
            evt.EVT_DATA_RECV,
            {'_is_cancelled' : is_cancelled, 'request' : msg}
        )
        assert event.is_cancelled is True

        event = evt.Event(
            None,
            evt.EVT_DATA_RECV,
            {'_is_cancelled' : is_cancelled, 'request' : msg2}
        )
        assert event.is_cancelled is False

    def test_assign_existing(self):
        """Test adding an attribute that already exists."""
        msg = r"'Event' object already has an attribute 'assoc'"
        with pytest.raises(AttributeError, match=msg):
            event = evt.Event(None, evt.EVT_C_STORE, {'assoc' : None})

    def test_action_information(self):
        """Test Event.action_information."""
        request = N_ACTION()
        request.ActionInformation = self.bytestream
        event = Event(
            None,
            evt.EVT_N_ACTION,
            {'request' : request, 'context' : self.context.as_tuple}
        )

        assert event._hash is None
        assert event._decoded is None
        ds = event.action_information
        assert event._hash == hash(request.ActionInformation)
        assert isinstance(ds, Dataset)
        assert ds.PatientName == 'TEST^Test'

        ds.PatientID = '1234567'
        assert event.action_information.PatientID == '1234567'

        # Test hash mismatch
        event._hash = None
        assert 'PatientID' not in event.action_information

    def test_attribute_list(self):
        """Test Event.attribute_list."""
        request = N_CREATE()
        request.AttributeList = self.bytestream
        event = Event(
            None,
            evt.EVT_N_CREATE,
            {'request' : request, 'context' : self.context.as_tuple}
        )

        assert event._hash is None
        assert event._decoded is None
        ds = event.attribute_list
        assert event._hash == hash(request.AttributeList)
        assert isinstance(ds, Dataset)
        assert ds.PatientName == 'TEST^Test'

        ds.PatientID = '1234567'
        assert event.attribute_list.PatientID == '1234567'

        # Test hash mismatch
        event._hash = None
        assert 'PatientID' not in event.attribute_list

    def test_event_information(self):
        """Test Event.event_information."""
        request = N_EVENT_REPORT()
        request.EventInformation = self.bytestream
        event = Event(
            None,
            evt.EVT_N_CREATE,
            {'request' : request, 'context' : self.context.as_tuple}
        )

        assert event._hash is None
        assert event._decoded is None
        ds = event.event_information
        assert event._hash == hash(request.EventInformation)
        assert isinstance(ds, Dataset)
        assert ds.PatientName == 'TEST^Test'

        ds.PatientID = '1234567'
        assert event.event_information.PatientID == '1234567'

        # Test hash mismatch
        event._hash = None
        assert 'PatientID' not in event.event_information

    def test_modification_list(self):
        """Test Event.modification_list."""
        request = N_SET()
        request.ModificationList = self.bytestream
        event = Event(
            None,
            evt.EVT_N_CREATE,
            {'request' : request, 'context' : self.context.as_tuple}
        )

        assert event._hash is None
        assert event._decoded is None
        ds = event.modification_list
        assert event._hash == hash(request.ModificationList)
        assert isinstance(ds, Dataset)
        assert ds.PatientName == 'TEST^Test'

        ds.PatientID = '1234567'
        assert event.modification_list.PatientID == '1234567'

        # Test hash mismatch
        event._hash = None
        assert 'PatientID' not in event.modification_list

    def test_empty_dataset(self):
        """Test with an empty dataset-like."""
        request = N_EVENT_REPORT()
        event = Event(
            None,
            evt.EVT_N_CREATE,
            {'request' : request, 'context' : self.context.as_tuple}
        )

        assert event._hash is None
        assert event._decoded is None
        ds = event.event_information
        assert event._hash == hash(request.EventInformation)
        assert ds == Dataset()

        # Test in-place modification works OK
        ds.PatientID = '1234567'
        assert event.event_information.PatientID == '1234567'

        # Test hash mismatch
        event._hash = None
        assert 'PatientID' not in event.event_information

    def test_empty_attr_identifiers(self):
        """Test with an empty attribute_identifiers."""
        request = N_GET()
        event = Event(
            None,
            evt.EVT_N_GET,
            {'request' : request, 'context' : self.context.as_tuple}
        )

        assert event.attribute_identifiers == []

    def test_attr_identifiers(self):
        """Test with attribute_identifiers."""
        request = N_GET()
        request.AttributeIdentifierList = [0x00100010, 0x00100020]
        event = Event(
            None,
            evt.EVT_N_GET,
            {'request' : request, 'context' : self.context.as_tuple}
        )

        tags = event.attribute_identifiers
        assert isinstance(tags[0], BaseTag)
        assert tags[0] == 0x00100010
        assert isinstance(tags[1], BaseTag)
        assert tags[1] == 0x00100020


# TODO: Should be able to remove in v1.4
INTERVENTION_HANDLERS = [
    _async_ops_handler, _sop_common_handler,
    _sop_extended_handler, _user_identity_handler, _c_echo_handler,
    _c_get_handler, _c_find_handler, _c_move_handler, _c_store_handler,
    _n_action_handler, _n_create_handler, _n_delete_handler,
    _n_event_report_handler, _n_get_handler, _n_set_handler
]

@pytest.mark.parametrize('handler', INTERVENTION_HANDLERS)
def test_default_handlers(handler):
    if handler not in [_sop_common_handler, _sop_extended_handler,
                       _c_echo_handler]:
        with pytest.raises(NotImplementedError):
            handler(None)
    else:
        handler(None)
