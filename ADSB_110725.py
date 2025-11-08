import csv
import io
import os
import sys
import time

import folium
# Matplotlib imports for plotting
import matplotlib
import requests
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout,
    QVBoxLayout, QSplitter, QPushButton
)
from haversine import haversine, Unit
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

# Ensure matplotlib uses the Qt5Agg backend
matplotlib.use('Qt5Agg')

# uv add requests folium haversine PyQt5 PyQtWebEngine matplotlib

# --- CONFIGURATION ---
# !!! YOU MUST CHANGE THESE VALUES !!!

# 1. Receiver Location (Latitude, Longitude)
# Used to center the map and calculate distance.
RECEIVER_LAT = XX.XXXX
RECEIVER_LON = -XX.XXXX

# 2. Raspberry Pi IP Address
# The local IP address of your Pi running dump1090.
PI_IP_ADDRESS = "XXX.XXX.X.XXX"  # <-- CHANGE THIS
# (You can often find this on your router's admin page)

# 3. dump1090 Data URL
# This is typically on port 8080 for dump1090-fa.
# If you use a different port, change it here.
# DATA_URL = f"http://{PI_IP_ADDRESS}:8080/data/aircraft.json"
# DATA_URL = f"http://{PI_IP_ADDRESS}/dump1090-fa/data/aircraft.json"
DATA_URL = f"http://{PI_IP_ADDRESS}:8504/data/aircraft.json"

# 4. Map Zoom Level
# 9 is a good starting point for a ~50-mile radius.
# Higher number = more zoomed in.
MAP_START_ZOOM = 9.5

# 5. Update Frequency (in milliseconds)
# 5000 ms = 5 seconds
UPDATE_INTERVAL_MS = 1000

# 6. Max Track Points
# Number of history points to store for each aircraft's track line
MAX_TRACK_POINTS = 10

# 7. Plot DC Airspace (0 = No, 1 = Yes)
# If 1, will plot the 30nm SFRA and 15nm FRZ rings
PLOT_DC_AIRSPACE = 0

# 8. Keep All Tracks (0 = No, 1 = Yes)
# 1 = Yes, keep all tracks (uses more memory over time)
# 0 = No, only show tracks for currently detected aircraft
KEEP_ALL_TRACKS = 0

# 9. Plot local airports on the map
# 1 = Yes, 0 = No
PLOT_AIRPORTS = 1

# Distance threshold for loading airports (in miles)
# Only airports within this distance from the receiver will be plotted
AIRPORT_DISTANCE = 100

# World borders painting distance (in miles)
# Only countries within this distance from the receiver will be plotted
WORLD_BORDER_DISTANCE = 200

# --- END CONFIGURATION ---

# --- Constants ---
# Center of the DC SFRA/FRZ
DCA_VOR_LAT = 38.859444
DCA_VOR_LON = -77.036389
SFRA_RADIUS_METERS = 55560  # 30 NM
FRZ_RADIUS_METERS = 27780  # 15 NM


class AdsbMapCanvas(FigureCanvas):
    """Matplotlib canvas for embedding in PyQt."""

    def __init__(self, parent=None, width=5, height=4, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi)
        # Set the figure background to black
        self.fig.patch.set_facecolor('black')
        super().__init__(self.fig)
        self.setParent(parent)


def getAirportLocations():
    # Get airports from https://davidmegginson.github.io/ourairports-data/airports.csv

    if PLOT_AIRPORTS == 0:
        return {}

    # Check if the file exists, if not download it
    if not os.path.exists('airports.csv'):
        try:
            url = 'https://davidmegginson.github.io/ourairports-data/airports.csv'
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            with open('airports.csv', 'w', encoding='utf-8') as f:
                f.write(response.text)
            print("Downloaded airports.csv")
        except Exception as e:
            print(f"Failed to download airports.csv: {e}")
            return {}

    airport_locations = {}
    try:
        with open('airports.csv', 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                # Filter for airports within reasonable distance from receiver
                try:
                    lat = float(row['latitude_deg'])
                    lon = float(row['longitude_deg'])

                    # Calculate distance from receiver
                    airport_pos = (lat, lon)
                    receiver_pos = (RECEIVER_LAT, RECEIVER_LON)
                    dist_miles = haversine(receiver_pos, airport_pos, unit=Unit.MILES)

                    # Only include airports within defined distance
                    if dist_miles <= AIRPORT_DISTANCE:
                        # Determine if towered or untowered (simple heuristic based on type)
                        airport_type = row.get('type', '')
                        if airport_type in ['large_airport', 'medium_airport']:
                            status = 'towered'
                        else:
                            status = 'untowered'

                        # Use ICAO code if available, otherwise use ident
                        code = row.get('icao_code') or row.get('ident', '')
                        if code:
                            airport_locations[code] = (lat, lon, status)

                except (ValueError, TypeError):
                    continue

        print(f"Loaded {len(airport_locations)} airports within 200 miles")

    except Exception as e:
        print(f"Error reading airports.csv: {e}")

    return airport_locations


def getWorldBorders():
    # Get data from Eurostat
    url = "https://gisco-services.ec.europa.eu/distribution/v2/countries/geojson/CNTR_RG_20M_2024_4326.geojson"

    # Check if the file exists, if not download it
    if not os.path.exists('world_borders.geojson'):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            with open('world_borders.geojson', 'w', encoding='utf-8') as f:
                f.write(response.text)
            print("Downloaded world_borders.geojson")
        except Exception as e:
            print(f"Failed to download world_borders.geojson: {e}")
            return None

    try:
        import json
        with open('world_borders.geojson', 'r', encoding='utf-8') as f:
            data = json.load(f)

        receiver_pos = (RECEIVER_LAT, RECEIVER_LON)
        filtered_features = []

        for feature in data.get('features', []):
            # Get geometry coordinates to check if country is nearby
            geometry = feature.get('geometry', {})
            if geometry.get('type') == 'Polygon':
                coords = geometry.get('coordinates', [[]])[0]
            elif geometry.get('type') == 'MultiPolygon':
                coords = []
                for polygon in geometry.get('coordinates', []):
                    coords.extend(polygon[0])
            else:
                continue

            # Check if any point in the geometry is within range
            within_range = False
            for coord in coords[:500]:  # Sample first 100 points for performance
                if len(coord) >= 2:
                    point_pos = (coord[1], coord[0])  # GeoJSON is [lon, lat]
                    try:
                        dist_miles = haversine(receiver_pos, point_pos, unit=Unit.MILES)
                        if dist_miles <= WORLD_BORDER_DISTANCE:
                            within_range = True
                            break
                    except:
                        continue

            if within_range:
                filtered_features.append(feature)

        # Create new GeoJSON with filtered features
        filtered_data = {
            'type': 'FeatureCollection',
            'features': filtered_features
        }

        print(f"Filtered to {len(filtered_features)} countries within 200nm")
        return json.dumps(filtered_data)

    except Exception as e:
        print(f"Error reading world_borders.geojson: {e}")
        return None


class AdsbTracker(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()

        # --- Airport Locations ---
        self.airport_location = getAirportLocations()

        # --- Data Storage ---
        # These lists will store all data cumulatively
        self.all_distances = []
        self.all_altitudes = []
        # --- MODIFICATION: Added storage for groundspeed ---
        self.all_groundspeeds = []

        # To track unique aircraft for smoother map updates
        self.current_aircraft = {}
        # To store position history for track lines
        self.aircraft_tracks = {}

        # --- State for UI toggles ---
        self.show_labels = True
        # --- ADDED: State for persistent zoom ---
        self.current_zoom = MAP_START_ZOOM

        self.world_data = getWorldBorders()

        self.initUI()

        # --- Setup Timer ---
        # This timer will trigger the data update
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_data)
        self.timer.start(UPDATE_INTERVAL_MS)

        # Run the first update immediately
        self.update_data()

    def initUI(self):
        """Initializes the main window layout."""

        self.setWindowTitle('Real-Time ADSB Tracker')
        self.setGeometry(100, 100, 1600, 900)  # x, y, width, height

        # --- Main Layout ---
        # A horizontal splitter divides the window into left (map) and right (plots)
        main_splitter = QSplitter(Qt.Horizontal)
        # Style the splitter handle to be black
        main_splitter.setStyleSheet("QSplitter::handle { background-color: black; }")

        # --- MODIFICATION: Left Side: Map and Controls ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)  # No space between map and control bar

        # --- 1. Map View (Top) ---
        self.map_view = QWebEngineView()
        # Set the view's background to black to make the "flash" on reload black
        self.map_view.setStyleSheet("background-color: black;")
        # Set the underlying web page's default background to black
        self.map_view.page().setBackgroundColor(Qt.black)
        self.map_view.setMinimumWidth(800)  # Give map a good default width

        # --- MODIFICATION: Add map with stretch factor 1 ---
        left_layout.addWidget(self.map_view, 1)

        # --- 2. Control Bar (Bottom) ---
        control_widget = QWidget()
        control_widget.setStyleSheet("background-color: black;")
        control_layout = QHBoxLayout(control_widget)
        control_layout.setContentsMargins(5, 5, 5, 5)  # Add a little padding

        # --- Label Toggle Button ---
        self.label_toggle_button = QPushButton("Labels: ON")
        self.label_toggle_button.setFlat(True)  # No border
        # Style to match aircraft count text (green, 12pt)
        self.label_toggle_button.setStyleSheet(
            "font-size: 12pt; color: green; background-color: black; text-align: left; padding: 5px;"
        )
        self.label_toggle_button.clicked.connect(self.toggle_labels)
        control_layout.addWidget(self.label_toggle_button)

        # Add a spacer to push zoom buttons to the right
        control_layout.addStretch(1)

        # --- ADDED: Zoom Buttons ---
        zoom_button_style = """
            QPushButton {
                font-size: 12pt; 
                color: green; 
                background-color: #333333; 
                border: 1px solid green;
                padding: 5px; 
                font-weight: bold;
            }
            QPushButton:pressed {
                background-color: #555555;
            }
        """

        self.zoom_out_button = QPushButton("-")
        self.zoom_out_button.setStyleSheet(zoom_button_style)
        self.zoom_out_button.setFixedWidth(40)
        self.zoom_out_button.clicked.connect(self.zoom_out)
        control_layout.addWidget(self.zoom_out_button)

        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setStyleSheet(zoom_button_style)
        self.zoom_in_button.setFixedWidth(40)
        self.zoom_in_button.clicked.connect(self.zoom_in)
        control_layout.addWidget(self.zoom_in_button)

        # --- MODIFICATION: Add control widget with stretch factor 0 ---
        left_layout.addWidget(control_widget, 0)

        main_splitter.addWidget(left_widget)  # Add the whole left widget
        # --- END MODIFICATION ---

        # --- Right Side: Plots ---
        right_widget = QWidget()
        # Set background of the plot area to black
        right_widget.setStyleSheet("background-color: black;")
        right_layout = QVBoxLayout(right_widget)

        # --- MODIFICATION: Create a 2x2 plot grid ---
        # A horizontal splitter divides the plot area
        main_plot_splitter = QSplitter(Qt.Horizontal)
        main_plot_splitter.setStyleSheet("QSplitter::handle { background-color: black; }")

        # --- Left Plot Column (Vertical Splitter) ---
        left_plot_splitter = QSplitter(Qt.Vertical)
        left_plot_splitter.setStyleSheet("QSplitter::handle { background-color: black; }")

        # Top-Left Plot: Altitude vs Distance Scatter
        self.scatter_dist_canvas = AdsbMapCanvas(self, width=5, height=4, dpi=100)
        self.scatter_dist_ax = self.scatter_dist_canvas.fig.add_subplot(111)
        left_plot_splitter.addWidget(self.scatter_dist_canvas)

        # --- SWAP 1 (Bottom-Left) ---
        # Bottom-Left Plot: Altitude vs Groundspeed Scatter (MOVED)
        self.scatter_gs_canvas = AdsbMapCanvas(self, width=5, height=4, dpi=100)
        self.scatter_gs_ax = self.scatter_gs_canvas.fig.add_subplot(111)
        left_plot_splitter.addWidget(self.scatter_gs_canvas)

        main_plot_splitter.addWidget(left_plot_splitter)  # Add left column

        # --- Right Plot Column (Vertical Splitter) ---
        right_plot_splitter = QSplitter(Qt.Vertical)
        right_plot_splitter.setStyleSheet("QSplitter::handle { background-color: black; }")

        # --- SWAP 2 (Top-Right) ---
        # Top-Right Plot: Altitude Histogram (MOVED)
        self.hist_alt_canvas = AdsbMapCanvas(self, width=5, height=4, dpi=100)
        self.hist_alt_ax = self.hist_alt_canvas.fig.add_subplot(111)
        right_plot_splitter.addWidget(self.hist_alt_canvas)

        # Bottom-Right Plot: Groundspeed Histogram (NEW)
        self.hist_gs_canvas = AdsbMapCanvas(self, width=5, height=4, dpi=100)
        self.hist_gs_ax = self.hist_gs_canvas.fig.add_subplot(111)
        right_plot_splitter.addWidget(self.hist_gs_canvas)

        main_plot_splitter.addWidget(right_plot_splitter)  # Add right column
        # --- END MODIFICATION ---

        right_layout.addWidget(main_plot_splitter)
        main_splitter.addWidget(right_widget)

        # Set initial size ratio (50% map, 50% plots)
        main_splitter.setSizes([800, 800])

        self.setCentralWidget(main_splitter)
        self.show()

    # --- Method to toggle label state ---
    def toggle_labels(self):
        """Toggles the visibility of aircraft labels."""
        self.show_labels = not self.show_labels
        if self.show_labels:
            self.label_toggle_button.setText("Labels: ON")
            # Style to match aircraft count text
            self.label_toggle_button.setStyleSheet(
                "font-size: 12pt; color: green; background-color: black; text-align: left; padding: 5px;"
            )
        else:
            self.label_toggle_button.setText("Labels: OFF")
            # Style with gray text to show "off" state
            self.label_toggle_button.setStyleSheet(
                "font-size: 12pt; color: #808080; background-color: black; text-align: left; padding: 5px;"
            )
        # The main update_data timer will automatically pick up this
        # state change on its next cycle.

    # --- ADDED: Methods to control zoom ---
    def zoom_in(self):
        """Increases the map zoom level, persisting on reload."""
        # Cap max zoom at 18
        self.current_zoom = min(18, self.current_zoom + 0.5)
        print(f"Zoom set to: {self.current_zoom}")

    def zoom_out(self):
        """Decreases the map zoom level, persisting on reload."""
        # Cap min zoom at 4
        self.current_zoom = max(4, self.current_zoom - 0.5)
        print(f"Zoom set to: {self.current_zoom}")

    # --- END ADDITION ---

    def fetch_aircraft_data(self):
        """Fetches and processes aircraft data from the receiver."""
        try:
            # Set a short timeout to avoid blocking the GUI
            response = requests.get(DATA_URL, timeout=2.0)
            response.raise_for_status()  # Raise an error for bad responses
            data = response.json()

            new_distances = []
            new_altitudes = []
            # --- MODIFICATION: Added list for new groundspeeds ---
            new_groundspeeds = []
            receiver_pos = (RECEIVER_LAT, RECEIVER_LON)

            # Use a temp dict to update aircraft positions
            temp_aircraft_seen = {}
            current_hex_codes = set()

            for ac in data.get('aircraft', []):
                # We need lat, lon, and altitude to plot
                lat = ac.get('lat')
                lon = ac.get('lon')

                # Use barometric altitude, fall back to geometric
                alt = ac.get('alt_baro', ac.get('alt_geom'))

                # Skip aircraft with no position or altitude
                if lat is None or lon is None or alt is None:
                    continue

                # Handle 'ground' value for altitude
                if alt == 'ground':
                    alt = 0

                # Ensure alt is in a number
                try:
                    alt_ft = float(alt)
                except ValueError:
                    continue

                # Calculate distance
                ac_pos = (lat, lon)
                dist_miles = haversine(receiver_pos, ac_pos, unit=Unit.MILES)

                # --- MODIFICATION: Get groundspeed ---
                gs_val = ac.get('gs')
                gs_float = None  # Default to None
                if gs_val is not None and gs_val != 'N/A':
                    try:
                        gs_float = float(gs_val)
                    except ValueError:
                        pass  # Keep gs_float as None if conversion fails

                # Add to our lists for this update cycle
                new_distances.append(dist_miles)
                new_altitudes.append(alt_ft)
                new_groundspeeds.append(gs_float)  # Append the float or None

                # Store for map
                hex_code = ac.get('hex', str(time.time()))  # Use time as fallback key
                current_hex_codes.add(hex_code)
                temp_aircraft_seen[hex_code] = {
                    'lat': lat,
                    'lon': lon,
                    'alt': alt_ft,
                    'flight': ac.get('flight', 'N/A').strip(),
                    # --- CHANGE 2: Store groundspeed ---
                    'gs': ac.get('gs', 'N/A')
                }

                # --- Track Line Logic ---
                # Get existing track, or a new empty list
                track = self.aircraft_tracks.get(hex_code, [])

                # Append new position
                track.append([lat, lon])

                # Limit track length
                if len(track) > MAX_TRACK_POINTS:
                    track = track[-MAX_TRACK_POINTS:]

                # Store the updated track
                self.aircraft_tracks[hex_code] = track

            # Update cumulative lists
            self.all_distances.extend(new_distances)
            self.all_altitudes.extend(new_altitudes)
            # --- MODIFICATION: Update cumulative groundspeeds ---
            self.all_groundspeeds.extend(new_groundspeeds)

            # Update the main aircraft dictionary
            self.current_aircraft = temp_aircraft_seen

            # Conditionally prune old tracks
            if KEEP_ALL_TRACKS == 0:
                # --- Prune old aircraft tracks ---
                # Remove tracks for aircraft that are no longer in the feed
                all_tracked_hex = list(self.aircraft_tracks.keys())
                for hex_code in all_tracked_hex:
                    if hex_code not in current_hex_codes:
                        del self.aircraft_tracks[hex_code]

            return True  # Success

        except requests.exceptions.RequestException as e:
            print(f"Error fetching data: {e}")
        except Exception as e:
            print(f"Error processing data: {e}")

        return False  # Failure

    def update_data(self):
        """Timer-driven function to update all UI elements."""
        if self.fetch_aircraft_data():
            # If data fetch was successful, update all GUI elements
            self.update_map()
            # --- MODIFICATION: Call all four plot updaters ---
            self.update_scatter_dist_plot()
            self.update_hist_alt_plot()
            self.update_scatter_gs_plot()
            self.update_hist_gs_plot()
        else:
            # Optional: handle failed update (e.g., show "Disconnected")
            print("Data update failed, skipping GUI refresh.")

    def update_map(self):
        """Refreshes the folium map with current aircraft positions."""

        # 1. Create a new map instance
        # --- MODIFIED: Use self.current_zoom instead of MAP_START_ZOOM ---
        m = folium.Map(location=[RECEIVER_LAT, RECEIVER_LON],
                       zoom_start=self.current_zoom,
                       tiles=None,  # Removed map tiles
                       zoom_control=False)  # Disable zoom buttons

        # --- UPDATED MODIFICATION: Inject CSS to force black background and hide Leaflet logo ---
        # This styles the HTML body AND the Leaflet map container
        black_bg_style = """
        <style>
            body { 
                background-color: black !important; 
            }
            .leaflet-container { 
                background-color: black !important; 
            }
            .leaflet-control-attribution {
                display: none !important;
            }
        </style>
        """
        m.get_root().header.add_child(folium.Element(black_bg_style))
        # --- END UPDATED MODIFICATION ---

        # --- CHANGE 3: Add Aircraft Count ---
        ac_count = len(self.current_aircraft)
        count_html = f"""
        <div style="position: fixed; 
                    bottom: 10px; 
                    left: 10px; 
                    z-index: 1000; 
                    font-family: Arial, sans-serif; 
                    font-size: 12pt; 
                    
                    color: green; 
                    background-color: rgba(0, 0, 0, 1);
                    padding: 5px 10px;
                    border-radius: 5px;">
            Aircraft: {ac_count}
        </div>
        """
        m.get_root().html.add_child(folium.Element(count_html))
        # --- END CHANGE 3 ---

        # --- Add US State Outlines for VA, MD, and DC ---

        # Define a style function for the GeoJSON layer
        state_style = lambda x: {
            'fillColor': 'none',  # No fill
            'color': '#FFFFFF',  # White outline
            'weight': 0.5,  # Thin line
            'fillOpacity': 0,  # No fill opacity
        }

        # --- MODIFICATION: Use combined state data ---
        if self.world_data:  # Only plot if data was loaded successfully
            folium.GeoJson(
                name='World Borders',
                style_function=state_style,
                overlay=True,
                control=False,  # Do not add to layer control
                # The property name in these files is 'NAME'
                #   tooltip=folium.features.GeoJsonTooltip(fields=['NAME']),
                highlight_function=lambda x: {'fillColor': '#00FF00', 'color': '#00FF00', 'weight': 3,
                                              'fillOpacity': 0.1},
                # Use the pre-combined data
                data=self.world_data,
            ).add_to(m)
        # --- END MODIFICATION ---

        # 2. Add a marker for the receiver (Triangle)
        folium.RegularPolygonMarker(
            location=[RECEIVER_LAT, RECEIVER_LON],
            popup="Receiver Location",
            number_of_sides=3,
            radius=8,
            rotation=30,  # Point-up triangle
            color="#FFFFFF",  # Green outline
            fill=True,
            fill_color="#000000",  # Black fill
            fill_opacity=1.0,
            weight=1
        ).add_to(m)

        # --- CHANGE 4: Add labeled distance rings ---
        DEG_LAT_PER_METER = 1 / 111111  # Approx

        # Radii from original code
        rings_to_plot = [
            (80467 / 5, " 10 mi"),  # 10 miles
            (2 * 80467 / 5, "20 mi"),  # 20 miles
            (3 * 80467 / 5, "30 mi"),  # 30 miles
            (4 * 80467 / 5, "40 mi"),  # 40 miles
            (80467, "50 mi")  # 50 miles
        ]

        for radius_m, label_txt in rings_to_plot:
            # Make rings white
            folium.Circle(
                location=[RECEIVER_LAT, RECEIVER_LON],
                radius=radius_m,
                color="#FFFFFF",  # White
                fill=False,
                opacity=0.75,
                weight=1
            ).add_to(m)

            # Add label at 6 o' clock
            # Calculate 6 o'clock position (approx)
            label_lat = RECEIVER_LAT - (radius_m * DEG_LAT_PER_METER)
            label_lon = RECEIVER_LON

            folium.Marker(
                location=[label_lat, label_lon],
                icon=folium.DivIcon(
                    icon_size=(50, 20),
                    icon_anchor=(25, 10),  # Center the icon on the lat/lon
                    html=(
                        f'<div style="font-size: 8pt; font-weight: bold;'
                        f'color: rgba(255, 255, 255, 0.75);'
                        f'background-color: black;'
                        f'padding: 2px 4px; border-radius: 3px; white-space: nowrap; '
                        f'display: flex; align-items: center; justify-content: center; '
                        f'width: 100%; height: 100%; box-sizing: border-box;">'
                        f'{label_txt}'
                        f'</div>'
                    )
                )
            ).add_to(m)

        # --- END CHANGE 4 ---

        # 4. Add DC Airspace if enabled
        if PLOT_DC_AIRSPACE == 1:
            # Add 30 NM SFRA Circle
            folium.Circle(
                location=[DCA_VOR_LAT, DCA_VOR_LON],
                radius=SFRA_RADIUS_METERS,
                color="red",
                weight=1,
                fill=True,
                fill_opacity=0 * 0.125 / 2,
                dash_array="4, 4",
                popup="DC SFRA (30 NM Ring)"
            ).add_to(m)

            # Add 15 NM FRZ Circle
            folium.Circle(
                location=[DCA_VOR_LAT, DCA_VOR_LON],
                radius=FRZ_RADIUS_METERS,
                color="red",
                weight=1,
                fill=True,
                fill_opacity=0 * 0.125 / 2,
                popup="DC FRZ (15 NM Ring)"
            ).add_to(m)

        # 5. Add markers for local airports if enabled
        if PLOT_AIRPORTS == 1:
            # Loop over new data structure: (lat, lon, status)
            for code, (lat, lon, status) in self.airport_location.items():

                # Set outline color based on tower status
                if status == "towered":
                    outline_color = "#FFFFFF"  # White
                    fill_color_set = "#FFFFFF"  # White
                else:
                    outline_color = "#404040"  # Gray
                    fill_color_set = "#404040"  # Gray

                folium.RegularPolygonMarker(
                    location=[lat, lon],
                    popup=code,
                    number_of_sides=4,  # Square
                    radius=6,
                    rotation=45,  # Rotate square to look like a diamond
                    color=outline_color,  # Use the conditional color
                    fill=True,
                    fill_color=fill_color_set,  # Black fill
                    fill_opacity=1.0,
                    weight=2
                ).add_to(m)

                # --- CHANGE 1: Add airport callsign text ---
                folium.Marker(
                    location=[lat, lon],
                    icon=folium.DivIcon(
                        icon_size=(150, 36),
                        icon_anchor=(0, 0),  # Anchor at top-left
                        # Style: 9pt, 500 weight, color from variable, 10px right, 7px up, no wrapping
                        html=f'<div style="font-size: 9pt; font-weight: 500; color: {outline_color}; margin-left: 10px; margin-top: -7px; white-space: nowrap;">{code}</div>',
                    )
                ).add_to(m)
                # --- END CHANGE 1 ---

        # 6. Conditionally plot aircraft and tracks
        if KEEP_ALL_TRACKS == 1:
            # --- Plotting Mode 1: Persistent Tracks ---

            # 6a. Add persistent track lines for ALL stored aircraft
            for hex_code, track in self.aircraft_tracks.items():
                if track and len(track) >= 2:
                    folium.PolyLine(
                        track,
                        color='#00FF00',  # Green
                        weight=2,
                        dash_array='2,4',
                        opacity=1
                    ).add_to(m)

            # 6b. Add markers for each CURRENTLY visible aircraft
            for hex_code, ac in self.current_aircraft.items():
                popup_html = (
                    f"<b>Flight: {ac['flight']}</b><br>"
                    f"Altitude: {ac['alt']:,} ft<br>"
                    f"Hex: {hex_code.upper()}"
                )

                # --- Add Aircraft Icon (Green Circle) ---
                folium.CircleMarker(
                    location=[ac['lat'], ac['lon']],
                    radius=3,
                    color='#00FF00',  # Green
                    weight=1.5,
                    fill=False,
                    fill_color='#000000',
                    fill_opacity=1.0,
                    popup=popup_html
                ).add_to(m)

                # --- MODIFICATION: Only draw labels if toggled ON ---
                if self.show_labels:
                    # --- CHANGE 2: Add Callsign, Alt, and Speed Text ---
                    # Prep for Alt/GS label
                    try:
                        alt_str = f"{int(ac['alt']):,}'"
                    except (ValueError, TypeError):
                        alt_str = "N/A"

                    try:
                        # Handle 'N/A' or missing gs
                        gs_val = float(ac['gs'])
                        gs_str = f"{int(gs_val)} kts"
                    except (ValueError, TypeError):
                        gs_str = "N/A"

                    alt_gs_label = f"{alt_str} @ {gs_str}"

                    folium.Marker(
                        location=[ac['lat'], ac['lon']],
                        icon=folium.DivIcon(
                            icon_size=(150, 36),
                            icon_anchor=(0, 0),  # Anchor at top-left
                            # Style text: 9pt, 500 weight, green, 10px right, 7px up, 2 lines
                            html=(
                                f'<div style="font-size: 9pt; font-weight: 500; color: #00FF00; margin-left: 10px; margin-top: -7px; line-height: 1.2;">'
                                f'<span style="white-space: nowrap;">{ac["flight"]}</span><br>'
                                f'<span style="white-space: nowrap;">{alt_gs_label}</span>'
                                f'</div>'
                            )
                        )
                    ).add_to(m)
                    # --- END CHANGE 2 ---

        else:
            # --- Plotting Mode 0: Only Current Tracks ---

            # 6c. Add markers and tracks for each CURRENTLY visible aircraft
            for hex_code, ac in self.current_aircraft.items():
                popup_html = (
                    f"<b>Flight: {ac['flight']}</b><br>"
                    f"Altitude: {ac['alt']:,} ft<br>"
                    f"Hex: {hex_code.upper()}"
                )

                # --- Add Track Line ---
                track = self.aircraft_tracks.get(hex_code)
                if track and len(track) >= 2:
                    folium.PolyLine(
                        track,
                        color='#00FF00',  # Green
                        dash_array='2,4',
                        weight=1

                    ).add_to(m)

                # --- Add Aircraft Icon (Green Circle) ---
                folium.CircleMarker(
                    location=[ac['lat'], ac['lon']],
                    radius=3,
                    color='#00FF00',  # Green
                    weight=1.5,
                    fill=False,
                    fill_color='#000000',
                    fill_opacity=1.0,
                    popup=popup_html
                ).add_to(m)

                # --- MODIFICATION: Only draw labels if toggled ON ---
                if self.show_labels:
                    # --- CHANGE 2: Add Callsign, Alt, and Speed Text ---
                    # Prep for Alt/GS label
                    try:
                        alt_str = f"{int(ac['alt']):,}'"
                    except (ValueError, TypeError):
                        alt_str = "N/A"

                    try:
                        # Handle 'N/A' or missing gs
                        gs_val = float(ac['gs'])
                        gs_str = f"{int(gs_val)} kts"
                    except (ValueError, TypeError):
                        gs_str = "N/A"

                    alt_gs_label = f"{alt_str} @ {gs_str}"

                    folium.Marker(
                        location=[ac['lat'], ac['lon']],
                        icon=folium.DivIcon(
                            icon_size=(150, 36),
                            icon_anchor=(0, 0),  # Anchor at top-left
                            # Style text: 9pt, 500 weight, green, 10px right, 7px up, 2 lines
                            html=(
                                f'<div style="font-size: 9pt; font-weight: 500; color: #00FF00; margin-left: 10px; margin-top: -7px; line-height: 1.2;">'
                                f'<span style="white-space: nowrap;">{ac["flight"]}</span><br>'
                                f'<span style="white-space: nowrap;">{alt_gs_label}</span>'
                                f'</div>'
                            )
                        )
                    ).add_to(m)
                    # --- END CHANGE 2 ---

        # 7. Save map to a temporary HTML buffer
        data = io.BytesIO()
        m.save(data, close_file=False)

        # 8. Load the HTML into the QWebEngineView
        self.map_view.setHtml(data.getvalue().decode())

    # --- MODIFICATION: Renamed function ---
    def update_scatter_dist_plot(self):
        """Refreshes the distance vs. altitude scatter plot."""
        # --- MODIFICATION: Use renamed axis ---
        self.scatter_dist_ax.clear()
        # Set background and face color
        self.scatter_dist_ax.set_facecolor('black')

        if self.all_distances:
            # 's=5' makes points small, 'alpha=0.3' makes them semi-transparent
            # Changed color to green
            self.scatter_dist_ax.scatter(
                self.all_distances,
                self.all_altitudes,
                s=1,
                alpha=0.5,
                c='#00FF00'  # Green
            )

        # self.scatter_dist_ax.set_title('Distance vs. Altitude', color='#00FF00') # <-- MODIFICATION: REMOVED
        self.scatter_dist_ax.set_xlabel('Distance from Receiver (miles)', color='#00FF00')
        self.scatter_dist_ax.set_ylabel('Altitude (feet)', color='#00FF00')
        self.scatter_dist_ax.set_ylim(bottom=0)
        self.scatter_dist_ax.set_xlim(left=0)
        self.scatter_dist_ax.grid(True, linestyle='--', alpha=0.3, color='gray')

        # Set tick colors
        self.scatter_dist_ax.tick_params(axis='x', colors='#00FF00')
        self.scatter_dist_ax.tick_params(axis='y', colors='#00FF00')

        # Set spine (border) colors
        for spine in self.scatter_dist_ax.spines.values():
            spine.set_edgecolor('#00FF00')

        # --- MODIFICATION: Add tight_layout to prevent cutoff ---
        self.scatter_dist_canvas.fig.tight_layout()

        # Redraw the canvas
        # --- MODIFICATION: Use renamed canvas ---
        self.scatter_dist_canvas.draw()

    # --- MODIFICATION: Renamed function ---
    def update_hist_alt_plot(self):
        """Refreshes the altitude distribution histogram."""
        # --- MODIFICATION: Use renamed axis ---
        self.hist_alt_ax.clear()
        # Set background and face color
        self.hist_alt_ax.set_facecolor('black')

        if self.all_altitudes:
            # Plot the cumulative histogram
            self.hist_alt_ax.hist(
                self.all_altitudes,
                bins=100,
                range=(0, 50000),
                color='#00FF00'  # Green
            )

        # self.hist_alt_ax.set_title('Altitude Distribution', color='#00FF00') # <-- MODIFICATION: REMOVED
        # --- THIS IS THE FIX ---
        self.hist_alt_ax.set_xlabel('Altitude (feet)', color='#00FF00')
        self.hist_alt_ax.set_ylabel('Aircraft Count', color='#00FF00')
        self.hist_alt_ax.set_xlim(0, 50000)

        # Set tick colors
        self.hist_alt_ax.tick_params(axis='x', colors='#00FF00')
        self.hist_alt_ax.tick_params(axis='y', colors='#00FF00')

        # Set spine (border) colors
        for spine in self.hist_alt_ax.spines.values():
            spine.set_edgecolor('#00FF00')

        # --- MODIFICATION: Add tight_layout to prevent cutoff ---
        self.hist_alt_canvas.fig.tight_layout()

        # Redraw the canvas
        # --- MODIFICATION: Use renamed canvas ---
        self.hist_alt_canvas.draw()

    # --- MODIFICATION: Added new function for GS scatter ---
    def update_scatter_gs_plot(self):
        """Refreshes the groundspeed vs. altitude scatter plot."""
        self.scatter_gs_ax.clear()
        self.scatter_gs_ax.set_facecolor('black')

        # Filter data to only include pairs where groundspeed is not None
        valid_data = [(gs, alt) for gs, alt in zip(self.all_groundspeeds, self.all_altitudes) if gs is not None]

        if valid_data:
            # Unzip the valid data into separate lists for plotting
            plot_gs, plot_alt = zip(*valid_data)

            self.scatter_gs_ax.scatter(
                plot_gs,
                plot_alt,
                s=1,
                alpha=0.5,
                c='#00FF00'  # Green
            )

        # self.scatter_gs_ax.set_title('Groundspeed vs. Altitude', color='#00FF00') # <-- MODIFICATION: REMOVED
        self.scatter_gs_ax.set_xlabel('Groundspeed (knots)', color='#00FF00')
        self.scatter_gs_ax.set_ylabel('Altitude (feet)', color='#00FF00')  # <-- MODIFICATION: REMOVED
        self.scatter_gs_ax.set_ylim(bottom=0)
        self.scatter_gs_ax.set_xlim(left=0)
        self.scatter_gs_ax.grid(True, linestyle='--', alpha=0.3, color='gray')

        # Set tick colors
        self.scatter_gs_ax.tick_params(axis='x', colors='#00FF00')
        self.scatter_gs_ax.tick_params(axis='y', colors='#00FF00')

        # Set spine (border) colors
        for spine in self.scatter_gs_ax.spines.values():
            spine.set_edgecolor('#00FF00')

        # --- MODIFICATION: Add tight_layout to prevent cutoff ---
        self.scatter_gs_canvas.fig.tight_layout()

        # Redraw the canvas
        self.scatter_gs_canvas.draw()

    # --- MODIFICATION: Added new function for GS histogram ---
    def update_hist_gs_plot(self):
        """Refreshes the groundspeed distribution histogram."""
        self.hist_gs_ax.clear()
        self.hist_gs_ax.set_facecolor('black')

        # Filter out None values from groundspeed list
        valid_gs = [gs for gs in self.all_groundspeeds if gs is not None]

        if valid_gs:
            # Plot the cumulative histogram
            self.hist_gs_ax.hist(
                valid_gs,
                bins=100,
                range=(0, 600),  # Set a reasonable max groundspeed
                color='#00FF00'  # Green
            )

        # self.hist_gs_ax.set_title('Groundspeed Distribution', color='#00FF00') # <-- MODIFICATION: REMOVED
        self.hist_gs_ax.set_xlabel('Groundspeed (knots)', color='#00FF00')
        self.hist_gs_ax.set_ylabel('Aircraft Count', color='#00FF00')  # <-- MODIFICATION: REMOVED
        self.hist_gs_ax.set_xlim(0, 600)

        # Set tick colors
        self.hist_gs_ax.tick_params(axis='x', colors='#00FF00')
        self.hist_gs_ax.tick_params(axis='y', colors='#00FF00')

        # Set spine (border) colors
        for spine in self.hist_gs_ax.spines.values():
            spine.set_edgecolor('#00FF00')

        # --- MODIFICATION: Add tight_layout to prevent cutoff ---
        self.hist_gs_canvas.fig.tight_layout()

        # Redraw the canvas
        self.hist_gs_canvas.draw()


if __name__ == '__main__':
    # Initialize the Qt Application
    app = QApplication(sys.argv)

    # Create and show the main window
    main_window = AdsbTracker()

    # Start the application's event loop
    sys.exit(app.exec_())
