#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Interface for Jupyter

:copyright:
    Mazama Science, IRIS
:license:
    GNU Lesser General Public License, Version 3
    (http://www.gnu.org/copyleft/lesser.html)
"""

from __future__ import (absolute_import, division, print_function)

# Basic packages
import os
import logging

# Pyweed UI components
from pyweed.preferences import Preferences, user_config_path
from pyweed.pyweed_utils import manage_cache, iter_channels, get_sncl, get_preferred_origin, DataRequest, get_distance
from obspy.clients.fdsn import Client
from pyweed.event_options import EventOptions
from pyweed.station_options import StationOptions
from pyweed.events_handler import EventsHandler
from pyweed.stations_handler import StationsHandler, StationsDataRequest
from PyQt4 import QtCore
from pyweed.waveforms_handler import WaveformsHandler
from pyweed.gui.PyWeedGUI import PyWeedGUI

LOGGER = logging.getLogger(__name__)


def resolve_data_center(self, data_center,
                        current_data_center, current_client,
                        other_data_center, other_client, services):
    """
    Helper for setting a data center. When we do this, we need to generate a corresponding Client,
    which can be expensive.
    """
    client = current_client
    if data_center != current_data_center or not current_client:
        # Use the other client if they're the same, otherwise create a client
        if data_center == other_data_center and other_client:
            client = other_client
        else:
            LOGGER.info("Creating ObsPy client for %s", data_center)
            client = Client(data_center)
        # Verify that this client supports station and dataselect
        for service in services:
            if service not in client.services:
                raise Exception("The %s data center does not provide a %s service" % (data_center, service))
    return (data_center, client)


class PyWeedJupyter():
    """
    This is the basic interface exposed to Jupyter.
    """

    gui = None

    preferences = None

    event_client = None
    _event_data_center = None
    event_options = None
    events_manager = None
    selected_event_ids = None

    station_client = None
    _station_data_center = None
    station_options = None
    stations_manager = None
    selected_station_ids = None

    def __init__(self):
        super(PyWeedJupyter, self).__init__()

        self.configure_logging()

        self.event_options = EventOptions()
        self.station_options = StationOptions()
        self.load_preferences()
        self.manage_cache()

        self.events = []
        self.selected_event_ids = []
        self.stations = []
        self.selected_station_ids = []

        self.events_handler = EventsHandler(self)
        self.stations_handler = StationsHandler(self)
        self.waveforms_handler = WaveformsHandler(self)

        self.gui = PyWeedGUI(self)

    def cleanup(self):
        self.manage_cache(init=False)
        self.save_preferences()

    @property
    def event_data_center(self):
        return self._event_data_center

    @event_data_center.setter
    def event_data_center(self, data_center):
        try:
            (data_center, client) = resolve_data_center(
                data_center, self._event_data_center, self.event_client,
                self._station_data_center, self.station_client, ['event'])
            self._event_data_center = data_center
            self.event_client = client
        except Exception:
            pass

    @property
    def station_data_center(self):
        return self._station_data_center

    @station_data_center.setter
    def station_data_center(self, data_center):
        try:
            (data_center, client) = resolve_data_center(
                data_center, self._station_data_center, self.station_client,
                self._event_data_center, self.event_client, ['station', 'dataselect'])
            self._station_data_center = data_center
            self.station_client = client
        except Exception:
            pass

    ###############
    # Actions
    ###############

    def load_events(self, options=None):
        """
        Launch a fetch operation for events
        """
        if options:
            # Simple request
            request = DataRequest(self.event_client, options)
        else:
            # Potentially complex request
            request = DataRequest(
                self.event_client,
                self.event_options.get_obspy_options()
            )
        self.events_handler.load_catalog(request)

    def load_stations(self, options=None):
        """
        Load stations
        """
        if options:
            # Simple request
            request = DataRequest(self.station_client, options)
        else:
            # Potentially complex request
            request = StationsDataRequest(
                self.station_client,
                self.station_options.get_obspy_options(),
                self.station_options.get_event_distances(),
                self.iter_selected_event_locations()
            )
        self.stations_handler.load_inventory(request)

    ###############
    # Access Data
    ###############

    @property
    def events_catalog(self):
        return self.events_manager.events

    def iter_selected_events(self):
        """
        Iterate over the selected events
        """
        if self.events_manager.catalog:
            for event in self.events_manager.catalog:
                event_id = event.resource_id.id
                if event_id in self.selected_event_ids:
                    yield event

    @property
    def stations_inventory(self):
        return self.stations_manager.inventory

    def iter_selected_stations(self):
        """
        Iterate over the selected stations (channels)
        Yields (network, station, channel) for each selected channel
        """
        if self.stations_manager.inventory:
            for (network, station, channel) in iter_channels(self.stations_manager.inventory):
                sncl = get_sncl(network, station, channel)
                if sncl in self.selected_station_ids:
                    yield (network, station, channel)

    ###############
    # Waveforms
    ###############

    def open_waveforms(self):
        self.gui.openWaveformsDialog()

    def iter_selected_events_stations(self):
        """
        Iterate through the selected event/station combinations.

        The main use case this method is meant to handle is where the user
        loaded stations based on selected events.

        For example, if the user selected multiple events and searched for stations
        within 20 degrees of any event, there may be stations that are within 20 degrees
        of one event but farther away from others -- we want to ensure that we only include
        the event/station combinations that are within 20 degrees of each other.
        """
        events = list(self.iter_selected_events())

        # Look for any event-based distance filter
        distance_range = self.station_options.get_event_distances()

        if distance_range:
            # Event locations by id
            event_locations = dict(self.iter_selected_event_locations())
        else:
            event_locations = {}

        # Iterate through the stations
        for (network, station, channel) in self.iter_selected_stations():
            for event in events:
                if distance_range:
                    event_location = event_locations.get(event.resource_id.id)
                    if event_location:
                        distance = get_distance(
                            event_location[0], event_location[1],
                            station.latitude, station.longitude)
                        LOGGER.debug(
                            "Distance from (%s, %s) to (%s, %s): %s",
                            event_location[0], event_location[1],
                            station.latitude, station.longitude,
                            distance
                        )
                        if distance < distance_range['mindistance'] or distance > distance_range['maxdistance']:
                            continue
                # If we reach here, include this event/station pair
                yield (event, network, station, channel)

    ###############
    # Misc
    ###############

    def load_preferences(self):
        """
        Load configurable preferences from ~/.pyweed/config.ini
        """
        LOGGER.info("Loading preferences")
        self.preferences = Preferences()
        try:
            self.preferences.load()
        except Exception as e:
            LOGGER.error("Unable to load configuration preferences -- using defaults.\n%s", e)
        self.event_options.set_options(self.preferences.EventOptions)
        self.event_data_center = self.preferences.Data.eventDataCenter
        self.station_options.set_options(self.preferences.StationOptions)
        self.station_data_center = self.preferences.Data.stationDataCenter

    def save_preferences(self):
        """
        Save preferences to ~/.pyweed/config.ini
        """
        LOGGER.info("Saving preferences")
        try:
            self.preferences.EventOptions.update(self.event_options.get_options(stringify=True))
            self.preferences.Data.eventDataCenter = self.event_data_center
            self.preferences.StationOptions.update(self.station_options.get_options(stringify=True))
            self.preferences.Data.stationDataCenter = self.station_data_center
            self.preferences.save()
        except Exception as e:
            LOGGER.error("Unable to save configuration preferences: %s", e)

    def configure_logging(self):
        """
        Configure the root logger
        """
        logger = logging.getLogger()
        try:
            log_level = getattr(logging, self.preferences.Logging.level)
            logger.setLevel(log_level)
        except Exception:
            logger.setLevel(logging.DEBUG)
        # Log to the terminal if available, otherwise log to a file
        if False:
            # Log to stderr
            handler = logging.StreamHandler()
        else:
            log_file = os.path.join(user_config_path(), 'pyweed.log')
            handler = logging.FileHandler(log_file, mode='w')
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        # handler.addFilter(NoConsoleLoggingFilter())
        logger.addHandler(handler)
        LOGGER.info("Logging configured")

    def manage_cache(self, init=True):
        """
        Make sure the waveform download directory exists and isn't full
        """
        download_path = self.preferences.Waveforms.downloadDir
        cache_size = int(self.preferences.Waveforms.cacheSize)
        LOGGER.info('Checking on download directory...')
        if os.path.exists(download_path):
            manage_cache(download_path, cache_size)
        elif init:
            try:
                os.makedirs(download_path, 0o700)
            except Exception as e:
                LOGGER.error("Creation of download directory failed with" + " error: \"%s\'""" % e)
                raise


if __name__ == "__main__":
    pyweed = PyWeedJupyter()
    # Do something?

