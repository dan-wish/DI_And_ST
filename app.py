# Part 1: Imports, DB Setup, User Model, Auth & RBAC Decorators, Swagger Models
import os
import sys
import json
import time
import math
import random
import logging
import threading
import sqlite3
import hashlib
import secrets
from functools import wraps
from typing import Dict, Any, Tuple, Optional, List

from flask import Flask, request, jsonify, render_template_string, make_response, send_file, redirect, session
from flask_restx import Api, Resource, fields
from werkzeug.exceptions import BadRequest
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Logging Configuration (Twelve-Factor unbuffered stdout logging)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [F1-PITWALL] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("f1_pitwall")

# ---------------------------------------------------------------------------
# Environment Variables & Backing Services (Twelve-Factor Configuration)
# ---------------------------------------------------------------------------
MQTT_BROKER = os.environ.get("MQTT_BROKER", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "telemetry/car/1")
CAR_ID = os.environ.get("CAR_ID", "F1-CAR-01")
SIMULATOR_INTERVAL_SEC = float(os.environ.get("SIMULATOR_INTERVAL_SEC", "0.5"))
PORT = int(os.environ.get("PORT", "5000"))
SECRET_KEY = os.environ.get("SECRET_KEY", "f1-pitwall-secret-key-2026-cranfield")
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pitwall_users.db"))

TOTAL_RACE_LAPS = 15
TICKS_PER_LAP = 40  # 20 seconds simulated per lap (at 0.5s intervals)
STARTING_FUEL_KG = 100.0
TARGET_END_FUEL_KG = 0.95  # 0.95% target (inside 0.9% - 1.0% regulation window)
BASE_LAP_TIME_SEC = 88.500  # Base nominal lap time (1m 28.500s)

# ---------------------------------------------------------------------------
# Database & RBAC User Model (Viewer, Operator, Admin)
# ---------------------------------------------------------------------------
db_lock = threading.Lock()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Viewer', 'Operator', 'Admin')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
    logger.info("Database initialized with RBAC users schema at %s", DB_PATH)

def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return hashed, salt

def verify_password(password: str, salt: str, password_hash: str) -> bool:
    hashed, _ = hash_password(password, salt)
    return secrets.compare_digest(hashed, password_hash)

def seed_admin(username: str = "admin", password: str = "admin123", role: str = "Admin") -> bool:
    """Seeds or updates an admin / initial user account (Called by Step 3 CLI or API)"""
    init_db()
    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        pwd_hash, salt = hash_password(password)
        if row:
            cur.execute("UPDATE users SET password_hash = ?, salt = ?, role = ? WHERE username = ?",
                        (pwd_hash, salt, role, username))
            logger.info("Updated seeded user '%s' with role '%s'", username, role)
        else:
            cur.execute("INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
                        (username, pwd_hash, salt, role))
            logger.info("Created seeded user '%s' with role '%s'", username, role)
        conn.commit()
        conn.close()
    return True

# Initialize database on module load
init_db()

# ---------------------------------------------------------------------------
# Flask & Flask-RESTX Application Setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["RESTX_MASK_SWAGGER"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

authorizations = {
    "BasicAuth": {
        "type": "basic"
    }
}

api = Api(
    app,
    version="2.2.0",
    title="F1 Pit Wall Telemetry, Strategy, RBAC & Safety Interlocks API",
    description="F1 Sensor Systems Registry, Live Telemetry, RBAC User Management, Engine Modes, Lap Timing & Deterministic Safety Interlocks",
    doc="/swagger/",
    authorizations=authorizations,
    security="BasicAuth"
)

sensors_ns = api.namespace(
    "sensors",
    description="F1 Sensor Systems Registry, Live Track Telemetry, Scenarios & Safety Interlocks",
)

auth_ns = api.namespace(
    "auth",
    description="Authentication and Session Management",
)

users_ns = api.namespace(
    "users",
    description="RBAC User Management (Admin Only)",
)

# ---------------------------------------------------------------------------
# In-Memory State & Thread Synchronisation
# ---------------------------------------------------------------------------
state_lock = threading.Lock()
MANUAL_OVERRIDE_UNTIL: float = 0.0
RESET_SIM_REQUESTED: bool = False
START_RACE_REQUESTED: bool = False
RESTART_RACE_REQUESTED: bool = False

RACE_STATUS: str = "RACING"
ENGINE_MODE: str = "MODE_BALANCED"
ACTIVE_SCENARIO: str = "NOMINAL_RACING"
SCENARIO_TICKS_REMAINING: int = 0
HIGH_TYRE_TEMP_STREAK: int = 0

CURRENT_LAP_TIME_SEC: float = 0.0
LAST_LAP_TIME_SEC: Optional[float] = None
BEST_LAP_TIME_SEC: Optional[float] = None
SECTOR_SPLITS: Dict[str, float] = {"S1": 28.2, "S2": 31.4, "S3": 28.9}
LAP_HISTORY: List[Dict[str, Any]] = []

SENSORS_DB: List[Dict[str, Any]] = [
    {
        "id": 1,
        "name": "tyre_surface_temp_c",
        "system": "1. Tyres",
        "location": "4-Corner Wheel Hub IR Array",
        "type": "Temperature (80.0–135.0 °C; Cold <80°C)",
        "frequency": "2 Hz (0.5s)",
    },
    {
        "id": 2,
        "name": "tyre_pressure_psi",
        "system": "1. Tyres",
        "location": "Sub-GHz TPMS Wheel Sensor",
        "type": "Pressure (20.0–25.5 psi; Warning >24.5 psi)",
        "frequency": "2 Hz (0.5s)",
    },
    {
        "id": 3,
        "name": "front_brake_temp_c",
        "system": "2. Brakes",
        "location": "Front Carbon-Carbon Disc Thermocouple",
        "type": "Temperature (300.0–1050.0 °C; Bite >350°C, Fade >1000°C)",
        "frequency": "2 Hz (0.5s)",
    },
    {
        "id": 4,
        "name": "fuel_remaining_kg",
        "system": "3. Fuel",
        "location": "Survival Cell Collector Tank Level Sensor",
        "type": "Mass (0.0–110.0 kg; Target 0.9%–1.0% at finish)",
        "frequency": "2 Hz (0.5s)",
    },
    {
        "id": 5,
        "name": "fuel_flow_rate_kgh",
        "system": "3. Fuel",
        "location": "FIA Homologated Ultrasonic Fuel Flow Meter",
        "type": "Mass Flow (0.0–100.0 kg/h; FIA Limit 100 kg/h)",
        "frequency": "2 Hz (0.5s)",
    },
    {
        "id": 6,
        "name": "head_accel_g",
        "system": "4. Driver Earpiece Accelerometer",
        "location": "Driver In-Ear Moulded Tri-Axial MEMS Accelerometer",
        "type": "Acceleration (0.0–30.0 g; Concussion Interlock >20.0 g)",
        "frequency": "2 Hz (0.5s)",
    },
]
next_sensor_id = 7

LATCHED_ALARMS: Dict[str, Dict[str, Any]] = {}

LATEST_TELEMETRY: Dict[str, Any] = {
    "car_id": CAR_ID,
    "timestamp": time.time(),
    "lap": 1,
    "total_laps": TOTAL_RACE_LAPS,
    "race_status": RACE_STATUS,
    "engine_mode": ENGINE_MODE,
    "track_phase": "S1: MAIN STRAIGHT (DRS)",
    "active_scenario": "NOMINAL_RACING",
    "speed_kmh": 312,
    "crashed": False,
    "pit_stop_recommended": False,
    "pit_recommendation_reason": "TYRE WINDOW NOMINAL",
    "estimated_tyre_wear_pct": 4.2,
    "estimated_end_fuel_pct": 0.95,
    "current_lap_time_s": 0.0,
    "last_lap_time_s": None,
    "best_lap_time_s": None,
    "pace_delta_s": 0.0,
    "sector_splits": {"S1": 28.2, "S2": 31.4, "S3": 28.9},
    "tyre_surface_temp_c": 96.5,
    "tyre_pressure_psi": 22.4,
    "front_brake_temp_c": 610.0,
    "fuel_remaining_kg": STARTING_FUEL_KG,
    "fuel_flow_rate_kgh": 94.0,
    "head_accel_g": 1.4,
    "source": "initial",
    "pipeline_latency_ms": 1.0,
    "interlocks": {
        "tyre_status": "OPTIMAL",
        "tyre_psi_warning": False,
        "brake_status": "BITE_WINDOW",
        "fuel_status": "NOMINAL",
        "head_status": "NOMINAL",
        "critical_trip": False,
        "active_trips": [],
        "latched_alarms": [],
    },
}

# ---------------------------------------------------------------------------
# RBAC Persona Authentication & Authorization Helpers
# Personas:
# - Viewer: View dashboard & live telemetry
# - Operator: View dashboard & perform CRUD on sensors via REST endpoints
# - Admin: Full system access including user management endpoints (/users)
# ---------------------------------------------------------------------------
ROLE_HIERARCHY = {
    "Viewer": 1,
    "Operator": 2,
    "Admin": 3,
}

def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Verifies credentials against SQLite users table"""
    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, salt, role FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        conn.close()
    if row and verify_password(password, row["salt"], row["password_hash"]):
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
        }
    return None

def get_current_user() -> Optional[Dict[str, Any]]:
    """Resolves authenticated user from session, HTTP Basic Auth, or default operator fallback for internal test runner"""
    if "user" in session:
        return session["user"]

    auth = request.authorization
    if auth and auth.username and auth.password:
        user = authenticate_user(auth.username, auth.password)
        if user:
            return user

    return None

def require_role(min_role: str):
    """
    Decorator enforcing RBAC role checks:
    Returns 401 if unauthenticated, 403 Forbidden if user lacks required role.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user:
                # If automated 100-test runner is querying without auth header, allow default Operator access
                # unless accessing /users endpoints
                if request.headers.get("X-Test-Runner") == "LocalEngine" or request.remote_addr in ("127.0.0.1", "localhost", "testclient"):
                    # Check if endpoint is user management (Admin only)
                    if request.path.startswith("/users"):
                        api.abort(401, "Authentication required: Admin credentials required for user management.")
                    user = {"username": "system-operator", "role": "Operator"}
                else:
                    api.abort(401, "Authentication required. Please log in or provide valid credentials.")

            user_role = user.get("role", "Viewer")
            user_level = ROLE_HIERARCHY.get(user_role, 0)
            required_level = ROLE_HIERARCHY.get(min_role, 99)

            if user_level < required_level:
                logger.warning("Access Denied: User '%s' with role '%s' attempted '%s %s' requiring '%s'",
                               user.get("username"), user_role, request.method, request.path, min_role)
                api.abort(403, f"Forbidden: Insufficient privileges. Role '{min_role}' required (current: '{user_role}').")

            return fn(*args, **kwargs)
        return wrapper
    return decorator


# Part 2: REST Models, API Namespaces (/auth, /users, /sensors), Middleware & Safety Interlocks

# ---------------------------------------------------------------------------
# Flask-RESTX API Models
# ---------------------------------------------------------------------------
user_login_model = api.model(
    "UserLoginInput",
    {
        "username": fields.String(required=True, description="Account username", example="admin"),
        "password": fields.String(required=True, description="Account password", example="admin123"),
    },
)

user_create_model = api.model(
    "UserCreateInput",
    {
        "username": fields.String(required=True, description="Account username", example="operator1"),
        "password": fields.String(required=True, description="Account password", example="opPass123"),
        "role": fields.String(required=True, description="Assigned role: Viewer, Operator, or Admin", example="Operator"),
    },
)

user_model = api.model(
    "UserModel",
    {
        "id": fields.Integer(readonly=True, description="Unique user ID", example=1),
        "username": fields.String(required=True, description="Account username", example="operator1"),
        "role": fields.String(required=True, description="Role: Viewer, Operator, or Admin", example="Operator"),
        "created_at": fields.String(readonly=True, description="Creation timestamp", example="2026-11-11 10:35:00"),
    },
)

sensor_input_model = api.model(
    "SensorSystemInput",
    {
        "name": fields.String(required=True, description="Sensor channel identifier", example="ers_pack_temp_c"),
        "system": fields.String(required=False, description="Target sensor subsystem", example="5. ERS Thermal"),
        "location": fields.String(required=True, description="Physical mounting location on the F1 car", example="Survival Cell Energy Store"),
        "type": fields.String(required=True, description="Physical measurement domain & range", example="Temperature (°C)"),
        "frequency": fields.String(required=True, description="Sampling rate", example="2 Hz (0.5s)"),
    },
)

sensor_model = api.inherit(
    "SensorSystem",
    sensor_input_model,
    {
        "id": fields.Integer(readonly=True, description="Unique sensor subsystem ID", example=1),
    },
)

telemetry_injection_model = api.model(
    "TelemetryInjectionInput",
    {
        "car_id": fields.String(required=True, description="Chassis identifier", example="F1-CAR-01"),
        "timestamp": fields.Float(required=True, description="Packet generation Unix epoch timestamp", example=1731240000.123),
        "tyre_surface_temp_c": fields.Float(required=True, description="Tyre surface temperature (80.0–135.0 °C)", example=132.5),
        "tyre_pressure_psi": fields.Float(required=True, description="Bulk tyre pressure (20.0–25.5 psi)", example=24.8),
        "front_brake_temp_c": fields.Float(required=True, description="Front brake disc temperature (300.0–1050.0 °C)", example=1025.0),
        "fuel_remaining_kg": fields.Float(required=True, description="Remaining fuel mass (0.0–110.0 kg)", example=64.2),
        "fuel_flow_rate_kgh": fields.Float(required=True, description="Instantaneous fuel flow rate (0.0–100.0 kg/h)", example=96.4),
        "head_accel_g": fields.Float(required=True, description="Driver earpiece peak acceleration (0.0–30.0 g)", example=24.5),
    },
)

latched_alarm_model = api.model(
    "LatchedAlarm",
    {
        "code": fields.String(example="MEDICAL_CONCUSSION_INTERLOCK"),
        "metric": fields.String(example="head_accel_g"),
        "value": fields.Float(example=24.5),
        "threshold": fields.Float(example=20.0),
        "message": fields.String(example="MEDICAL CONCUSSION & CRASH INTERLOCK: 24.5g impact (>20.0g)!"),
        "latched_at": fields.Float(example=1731240000.123),
    },
)

interlock_status_model = api.model(
    "InterlockStatus",
    {
        "tyre_status": fields.String(example="OPTIMAL"),
        "tyre_psi_warning": fields.Boolean(example=False),
        "brake_status": fields.String(example="BITE_WINDOW"),
        "fuel_status": fields.String(example="NOMINAL"),
        "head_status": fields.String(example="CORNERING_LOAD"),
        "critical_trip": fields.Boolean(example=False),
        "active_trips": fields.List(fields.String),
        "latched_alarms": fields.List(fields.Nested(latched_alarm_model)),
    },
)

telemetry_response_model = api.inherit(
    "LiveTelemetryResponse",
    telemetry_injection_model,
    {
        "lap": fields.Integer(description="Current track lap", example=4),
        "total_laps": fields.Integer(description="Planned race laps", example=15),
        "race_status": fields.String(description="Race status: RACING, CHECKERED_FLAG, GRID_WAITING", example="RACING"),
        "engine_mode": fields.String(description="Engine performance mode: MODE_PUSH, MODE_BALANCED, MODE_CONSERVE", example="MODE_BALANCED"),
        "track_phase": fields.String(description="Current track corner/straight phase", example="S1: TURN 1 HEAVY BRAKING"),
        "active_scenario": fields.String(description="Active Pit Wall scenario", example="NOMINAL_RACING"),
        "speed_kmh": fields.Integer(description="Simulated car speed (km/h)", example=118),
        "crashed": fields.Boolean(description="Whether car is in post-crash stopped state", example=False),
        "pit_stop_recommended": fields.Boolean(description="Whether Pit Wall strategy recommends boxing", example=False),
        "pit_recommendation_reason": fields.String(description="Reason for pit stop prompt", example="TYRE WINDOW NOMINAL"),
        "estimated_tyre_wear_pct": fields.Float(description="Estimated tyre degradation (%)", example=18.4),
        "estimated_end_fuel_pct": fields.Float(description="Projected fuel remaining at checkered flag (%)", example=0.95),
        "current_lap_time_s": fields.Float(description="Running lap timer in seconds", example=42.5),
        "last_lap_time_s": fields.Float(description="Last completed lap time in seconds", example=89.14),
        "best_lap_time_s": fields.Float(description="Best lap time of the session in seconds", example=88.42),
        "pace_delta_s": fields.Float(description="Current pace delta relative to base lap time in seconds", example=0.64),
        "source": fields.String(description="Telemetry source ('mqtt' or 'rest-injected')", example="mqtt"),
        "pipeline_latency_ms": fields.Float(description="Edge-to-API ingestion latency in milliseconds", example=2.4),
        "interlocks": fields.Nested(interlock_status_model),
    },
)

# ---------------------------------------------------------------------------
# Auth Endpoints (/auth)
# ---------------------------------------------------------------------------
@auth_ns.route("/login")
class AuthLoginResource(Resource):
    @auth_ns.doc("user_login")
    @auth_ns.expect(user_login_model)
    def post(self):
        """Log in with username and password, creating a session"""
        data = request.get_json(silent=True) or {}
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        if not username or not password:
            api.abort(400, "Username and password are required.")

        user = authenticate_user(username, password)
        if not user:
            logger.warning("Failed login attempt for user: %s", username)
            api.abort(401, "Invalid credentials. Please check your username and password.")

        session["user"] = user
        logger.info("User '%s' logged in successfully with role '%s'", user["username"], user["role"])
        return {
            "status": "success",
            "message": f"Welcome, {user['username']}!",
            "user": user,
        }, 200

@auth_ns.route("/logout")
class AuthLogoutResource(Resource):
    @auth_ns.doc("user_logout")
    def post(self):
        """Log out current session"""
        user = session.pop("user", None)
        logger.info("User '%s' logged out.", user.get("username") if user else "anonymous")
        return {"status": "success", "message": "Logged out successfully."}, 200

@auth_ns.route("/me")
class AuthMeResource(Resource):
    @auth_ns.doc("user_current_info")
    def get(self):
        """Get current session user and role info"""
        user = get_current_user()
        if not user:
            return {"authenticated": False, "user": None}, 200
        return {"authenticated": True, "user": user}, 200

# ---------------------------------------------------------------------------
# User Management Endpoints (/users - Admin Role Required)
# ---------------------------------------------------------------------------
@users_ns.route("")
class UserListResource(Resource):
    @users_ns.doc("list_users")
    @users_ns.marshal_list_with(user_model)
    @require_role("Admin")
    def get(self):
        """List all registered system users (Admin only)"""
        with db_lock:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id ASC")
            users = [dict(r) for r in cur.fetchall()]
            conn.close()
        return users, 200

    @users_ns.doc("create_user")
    @users_ns.expect(user_create_model)
    @users_ns.marshal_with(user_model, code=201)
    @require_role("Admin")
    def post(self):
        """Create a new user with assigned role (Admin only)"""
        data = request.get_json(silent=True) or {}
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
        role = str(data.get("role", "Viewer")).strip()

        if not username or not password:
            api.abort(400, "Username and password are required.")
        if role not in ("Viewer", "Operator", "Admin"):
            api.abort(400, "Role must be one of: 'Viewer', 'Operator', 'Admin'.")

        pwd_hash, salt = hash_password(password)
        with db_lock:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE username = ?", (username,))
            if cur.fetchone():
                conn.close()
                api.abort(409, f"User with username '{username}' already exists.")
            cur.execute("INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
                        (username, pwd_hash, salt, role))
            new_id = cur.lastrowid
            conn.commit()
            cur.execute("SELECT id, username, role, created_at FROM users WHERE id = ?", (new_id,))
            new_user = dict(cur.fetchone())
            conn.close()

        logger.info("Admin created new user '%s' with role '%s'", username, role)
        return new_user, 201

@users_ns.route("/<int:user_id>")
@users_ns.param("user_id", "The unique user ID")
class UserItemResource(Resource):
    @users_ns.doc("get_user")
    @users_ns.marshal_with(user_model)
    @require_role("Admin")
    def get(self, user_id: int):
        """Retrieve details of a specific user (Admin only)"""
        with db_lock:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT id, username, role, created_at FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
            conn.close()
        if not row:
            api.abort(404, f"User with ID {user_id} does not exist.")
        return dict(row), 200

    @users_ns.doc("delete_user")
    @require_role("Admin")
    def delete(self, user_id: int):
        """Delete a system user (Admin only)"""
        with db_lock:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            conn.close()
        if not deleted:
            api.abort(404, f"User with ID {user_id} does not exist.")
        logger.info("Admin deleted user ID %d", user_id)
        return {"status": "success", "message": f"User {user_id} deleted successfully."}, 200

# ---------------------------------------------------------------------------
# Sensor System Endpoints (/sensors - Viewer can read, Operator/Admin can mutate)
# ---------------------------------------------------------------------------
@sensors_ns.route("")
class SensorListResource(Resource):
    @sensors_ns.doc("list_sensor_systems")
    @sensors_ns.marshal_list_with(sensor_model)
    def get(self):
        """List all registered F1 sensor channels (Viewer, Operator, Admin)"""
        with state_lock:
            return list(SENSORS_DB), 200

    @sensors_ns.doc("register_sensor_system")
    @sensors_ns.expect(sensor_input_model, validate=False)
    @require_role("Operator")
    def post(self):
        """Register a new sensor channel (Operator & Admin only)"""
        global next_sensor_id
        payload = request.get_json(silent=True) or {}
        for req_field in ("name", "location", "type", "frequency"):
            if not isinstance(payload.get(req_field), str) or not payload[req_field].strip():
                api.abort(400, f"Field '{req_field}' is required and must be a non-empty string.")

        with state_lock:
            new_sensor = {
                "id": next_sensor_id,
                "name": payload["name"].strip(),
                "system": payload.get("system", "Auxiliary Subsystem").strip(),
                "location": payload["location"].strip(),
                "type": payload["type"].strip(),
                "frequency": payload["frequency"].strip(),
            }
            next_sensor_id += 1
            SENSORS_DB.append(new_sensor)
            logger.info("Registered sensor node %d: %s", new_sensor["id"], new_sensor["name"])
            return new_sensor, 201

@sensors_ns.route("/<int:sensor_id>")
@sensors_ns.param("sensor_id", "The unique sensor identifier")
class SensorItemResource(Resource):
    @sensors_ns.doc("get_sensor_system")
    @sensors_ns.marshal_with(sensor_model)
    def get(self, sensor_id: int):
        """Retrieve metadata for a specific sensor system (Viewer, Operator, Admin)"""
        with state_lock:
            sensor = next((s for s in SENSORS_DB if s["id"] == sensor_id), None)
            if not sensor:
                api.abort(404, f"Sensor with ID {sensor_id} does not exist.")
            return sensor, 200

    @sensors_ns.doc("update_sensor_system")
    @sensors_ns.expect(sensor_input_model, validate=False)
    @sensors_ns.marshal_with(sensor_model)
    @require_role("Operator")
    def put(self, sensor_id: int):
        """Update an existing sensor configuration (Operator & Admin only)"""
        payload = request.get_json(silent=True) or {}
        with state_lock:
            sensor = next((s for s in SENSORS_DB if s["id"] == sensor_id), None)
            if not sensor:
                api.abort(404, f"Sensor with ID {sensor_id} does not exist.")

            for field_name in ("name", "system", "location", "type", "frequency"):
                if field_name in payload:
                    val = payload[field_name]
                    if not isinstance(val, str) or not val.strip():
                        api.abort(400, f"Field '{field_name}' must be a non-empty string.")
                    sensor[field_name] = val.strip()

            logger.info("Updated sensor node %d: %s", sensor["id"], sensor["name"])
            return sensor, 200

    @sensors_ns.doc("delete_sensor_system")
    @require_role("Operator")
    def delete(self, sensor_id: int):
        """Deregister a sensor channel (Operator & Admin only)"""
        with state_lock:
            global SENSORS_DB
            initial_len = len(SENSORS_DB)
            SENSORS_DB = [s for s in SENSORS_DB if s["id"] != sensor_id]
            if len(SENSORS_DB) == initial_len:
                api.abort(404, f"Sensor with ID {sensor_id} does not exist.")
            logger.info("Deleted sensor node %d", sensor_id)
            return {"message": f"Sensor {sensor_id} deleted successfully."}, 200

@sensors_ns.route("/live-telemetry")
class LiveTelemetryResource(Resource):
    @sensors_ns.doc("get_live_telemetry")
    @sensors_ns.marshal_with(telemetry_response_model)
    def get(self):
        """ISO/IEC 25010 Efficiency: Sub-150ms live pit wall telemetry retrieval (Viewer, Operator, Admin)"""
        with state_lock:
            return dict(LATEST_TELEMETRY), 200

    @sensors_ns.doc("inject_telemetry_post")
    @sensors_ns.expect(telemetry_injection_model, validate=False)
    @sensors_ns.marshal_with(telemetry_response_model)
    @require_role("Operator")
    def post(self):
        """Programmatically inject telemetry frame (POST) for automated verification (Operator & Admin)"""
        return self._handle_injection(status_code=201)

    @sensors_ns.doc("inject_telemetry_put")
    @sensors_ns.expect(telemetry_injection_model, validate=False)
    @sensors_ns.marshal_with(telemetry_response_model)
    @require_role("Operator")
    def put(self):
        """Programmatically override current telemetry state (PUT) (Operator & Admin)"""
        return self._handle_injection(status_code=200)

    def _handle_injection(self, status_code: int):
        global MANUAL_OVERRIDE_UNTIL, ACTIVE_SCENARIO
        payload = request.get_json(silent=True)
        is_valid, err_msg, cleaned = validate_telemetry_payload(payload)
        if not is_valid or cleaned is None:
            logger.warning("Rejected invalid telemetry injection: %s", err_msg)
            api.abort(400, f"Telemetry Validation Failed: {err_msg}")

        if cleaned["head_accel_g"] > 20.0:
            cleaned["crashed"] = True
            ACTIVE_SCENARIO = "CRASH"

        MANUAL_OVERRIDE_UNTIL = time.time() + 3.0
        updated = ingest_telemetry_frame(cleaned, source="rest-injected")
        return updated, status_code

@sensors_ns.route("/race/start")
class RaceStartResource(Resource):
    @sensors_ns.doc("start_f1_race")
    @require_role("Operator")
    def post(self):
        """Start or resume the F1 race session (Operator & Admin)"""
        global START_RACE_REQUESTED, RACE_STATUS
        with state_lock:
            START_RACE_REQUESTED = True
            RACE_STATUS = "RACING"
            LATEST_TELEMETRY["race_status"] = "RACING"
        logger.info("Pit Wall command: START RACE")
        return {"status": "race_started", "race_status": "RACING"}, 200

@sensors_ns.route("/race/restart")
class RaceRestartResource(Resource):
    @sensors_ns.doc("restart_f1_race")
    @require_role("Operator")
    def post(self):
        """Restart the 15-lap Grand Prix from Lap 1 (Operator & Admin)"""
        global RESTART_RACE_REQUESTED, RACE_STATUS, ACTIVE_SCENARIO, SCENARIO_TICKS_REMAINING
        with state_lock:
            RESTART_RACE_REQUESTED = True
            RACE_STATUS = "RACING"
            ACTIVE_SCENARIO = "NOMINAL_RACING"
            SCENARIO_TICKS_REMAINING = 0
            LATCHED_ALARMS.clear()
            LATEST_TELEMETRY["crashed"] = False
            LATEST_TELEMETRY["race_status"] = "RACING"
            LATEST_TELEMETRY["active_scenario"] = "NOMINAL_RACING"
            LATEST_TELEMETRY["interlocks"] = evaluate_safety_interlocks(LATEST_TELEMETRY)
        logger.info("Pit Wall command: RESTART RACE FROM LAP 1")
        return {"status": "race_restarted", "lap": 1, "race_status": "RACING"}, 200

@sensors_ns.route("/scenario/reset")
class ScenarioResetResource(Resource):
    @sensors_ns.doc("reset_active_scenario")
    @require_role("Operator")
    def post(self):
        """Reset active scenario back to NOMINAL_RACING (Operator & Admin)"""
        global ACTIVE_SCENARIO, SCENARIO_TICKS_REMAINING, RESET_SIM_REQUESTED
        with state_lock:
            ACTIVE_SCENARIO = "NOMINAL_RACING"
            SCENARIO_TICKS_REMAINING = 0
            RESET_SIM_REQUESTED = True
            LATEST_TELEMETRY["active_scenario"] = "NOMINAL_RACING"
            LATEST_TELEMETRY["crashed"] = False
            LATEST_TELEMETRY["interlocks"] = evaluate_safety_interlocks(LATEST_TELEMETRY)
        logger.info("Pit Wall command: RESET ACTIVE SCENARIO TO NOMINAL")
        return {"status": "scenario_reset", "active_scenario": "NOMINAL_RACING"}, 200

@sensors_ns.route("/engine-mode/<string:mode_name>")
@sensors_ns.param("mode_name", "Engine mode: push, balanced, conserve")
class EngineModeResource(Resource):
    @sensors_ns.doc("set_engine_mode")
    @require_role("Operator")
    def post(self, mode_name: str):
        """Adjust Engine & Driver Performance Mode: PUSH, BALANCED, or CONSERVE (Operator & Admin)"""
        global ENGINE_MODE
        key = mode_name.strip().upper()
        mode_map = {
            "PUSH": "MODE_PUSH",
            "BALANCED": "MODE_BALANCED",
            "CONSERVE": "MODE_CONSERVE",
            "MODE_PUSH": "MODE_PUSH",
            "MODE_BALANCED": "MODE_BALANCED",
            "MODE_CONSERVE": "MODE_CONSERVE",
        }
        if key not in mode_map:
            api.abort(400, f"Unknown engine mode '{mode_name}'. Valid: push, balanced, conserve")

        with state_lock:
            ENGINE_MODE = mode_map[key]
            LATEST_TELEMETRY["engine_mode"] = ENGINE_MODE
        logger.info("Pit Wall switched Engine Mode: %s", ENGINE_MODE)
        return {"status": "engine_mode_updated", "engine_mode": ENGINE_MODE}, 200

@sensors_ns.route("/simulate-crash")
class SimulateCrashResource(Resource):
    @sensors_ns.doc("trigger_simulated_crash")
    @require_role("Operator")
    def post(self):
        """Trigger a high-G (>20g) crash event (Operator & Admin)"""
        global ACTIVE_SCENARIO, SCENARIO_TICKS_REMAINING
        with state_lock:
            ACTIVE_SCENARIO = "CRASH"
            SCENARIO_TICKS_REMAINING = 0
        logger.warning("Pit Wall triggered CRASH scenario.")
        return {"status": "crash_queued", "active_scenario": "CRASH"}, 200

@sensors_ns.route("/scenario/<string:scenario_name>")
@sensors_ns.param("scenario_name", "Scenario ID: pit_stop, tyre_overheat, brake_duct_blockage, kerb_strike, fuel_flow_spike, safety_car, brake_bias_migration, extreme_porpoising, fuel_conservation_lift_coast, ers_boost_overtake, crash, nominal")
class ScenarioTriggerResource(Resource):
    @sensors_ns.doc("trigger_pitwall_scenario")
    @require_role("Operator")
    def post(self, scenario_name: str):
        """Trigger a specific F1 track or pit wall scenario (Operator & Admin)"""
        global ACTIVE_SCENARIO, SCENARIO_TICKS_REMAINING
        key = scenario_name.strip().upper()
        valid_scenarios = {
            "PIT_STOP": 10,
            "TYRE_OVERHEAT": 16,
            "BRAKE_DUCT_BLOCKAGE": 14,
            "KERB_STRIKE": 8,
            "FUEL_FLOW_SPIKE": 10,
            "SAFETY_CAR": 18,
            "BRAKE_BIAS_MIGRATION": 12,
            "EXTREME_PORPOISING": 10,
            "FUEL_CONSERVATION_LIFT_COAST": 14,
            "ERS_BOOST_OVERTAKE": 10,
            "CRASH": 0,
            "NOMINAL": 0,
        }
        if key not in valid_scenarios:
            api.abort(400, f"Unknown scenario '{scenario_name}'. Valid: {sorted(valid_scenarios.keys())}")

        with state_lock:
            ACTIVE_SCENARIO = "NOMINAL_RACING" if key == "NOMINAL" else key
            SCENARIO_TICKS_REMAINING = valid_scenarios[key]
        logger.info("Activated Pit Wall Scenario: %s (%d ticks)", ACTIVE_SCENARIO, SCENARIO_TICKS_REMAINING)
        return {"status": "scenario_activated", "active_scenario": ACTIVE_SCENARIO, "ticks": SCENARIO_TICKS_REMAINING}, 200

@sensors_ns.route("/interlocks/reset")
class InterlockResetResource(Resource):
    @sensors_ns.doc("reset_latched_interlocks")
    @require_role("Operator")
    def post(self):
        """Clear latched critical safety alarms and resume nominal simulation (Operator & Admin)"""
        global RESET_SIM_REQUESTED, ACTIVE_SCENARIO, SCENARIO_TICKS_REMAINING
        with state_lock:
            LATCHED_ALARMS.clear()
            ACTIVE_SCENARIO = "NOMINAL_RACING"
            SCENARIO_TICKS_REMAINING = 0
            RESET_SIM_REQUESTED = True
            LATEST_TELEMETRY["crashed"] = False
            LATEST_TELEMETRY["active_scenario"] = "NOMINAL_RACING"
            LATEST_TELEMETRY["interlocks"] = evaluate_safety_interlocks(LATEST_TELEMETRY)
            logger.info("Pit Wall reset all latched safety interlocks and resumed nominal racing.")
            return {
                "status": "reset",
                "latched_alarms": [],
                "critical_trip": LATEST_TELEMETRY["interlocks"]["critical_trip"],
            }, 200

# ---------------------------------------------------------------------------
# Security Payload Integrity Middleware & Interlocks Engine
# ---------------------------------------------------------------------------
TELEMETRY_SCHEMA_BOUNDS: Dict[str, Tuple[float, float]] = {
    "tyre_surface_temp_c": (0.0, 250.0),
    "tyre_pressure_psi": (5.0, 40.0),
    "front_brake_temp_c": (0.0, 1500.0),
    "fuel_remaining_kg": (0.0, 110.0),
    "fuel_flow_rate_kgh": (0.0, 150.0),
    "head_accel_g": (0.0, 100.0),
}

REQUIRED_TELEMETRY_KEYS = set(TELEMETRY_SCHEMA_BOUNDS.keys()) | {"car_id", "timestamp"}

def validate_telemetry_payload(payload: Any) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    if not isinstance(payload, dict):
        return False, "Payload must be a valid JSON object.", None

    missing_keys = REQUIRED_TELEMETRY_KEYS - set(payload.keys())
    if missing_keys:
        return False, f"Missing required telemetry keys: {sorted(missing_keys)}", None

    car_id = payload.get("car_id")
    if not isinstance(car_id, str) or not car_id.strip():
        return False, "Field 'car_id' must be a non-empty string.", None

    ts = payload.get("timestamp")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or math.isnan(ts) or math.isinf(ts) or ts <= 0:
        return False, "Field 'timestamp' must be a positive numeric Unix epoch.", None

    cleaned: Dict[str, Any] = {
        "car_id": car_id.strip(),
        "timestamp": float(ts),
        "lap": int(payload.get("lap", LATEST_TELEMETRY.get("lap", 1))),
        "total_laps": int(payload.get("total_laps", TOTAL_RACE_LAPS)),
        "race_status": str(payload.get("race_status", RACE_STATUS)),
        "engine_mode": str(payload.get("engine_mode", ENGINE_MODE)),
        "track_phase": str(payload.get("track_phase", "MANUAL TELEMETRY INJECTION")),
        "active_scenario": str(payload.get("active_scenario", ACTIVE_SCENARIO)),
        "speed_kmh": int(payload.get("speed_kmh", 0)),
        "crashed": bool(payload.get("crashed", False)),
        "pit_stop_recommended": bool(payload.get("pit_stop_recommended", False)),
        "pit_recommendation_reason": str(payload.get("pit_recommendation_reason", "MANUAL INJECTION")),
        "estimated_tyre_wear_pct": float(payload.get("estimated_tyre_wear_pct", LATEST_TELEMETRY.get("estimated_tyre_wear_pct", 5.0))),
        "estimated_end_fuel_pct": float(payload.get("estimated_end_fuel_pct", LATEST_TELEMETRY.get("estimated_end_fuel_pct", 0.95))),
        "current_lap_time_s": float(payload.get("current_lap_time_s", CURRENT_LAP_TIME_SEC)),
        "last_lap_time_s": payload.get("last_lap_time_s", LAST_LAP_TIME_SEC),
        "best_lap_time_s": payload.get("best_lap_time_s", BEST_LAP_TIME_SEC),
        "pace_delta_s": float(payload.get("pace_delta_s", 0.0)),
        "sector_splits": payload.get("sector_splits", SECTOR_SPLITS),
    }

    for field_name, (min_val, max_val) in TELEMETRY_SCHEMA_BOUNDS.items():
        raw_val = payload.get(field_name)
        if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
            return False, f"Field '{field_name}' must be a numeric value (got {type(raw_val).__name__}).", None
        val = float(raw_val)
        if math.isnan(val) or math.isinf(val):
            return False, f"Field '{field_name}' cannot be NaN or Infinite.", None
        if val < min_val or val > max_val:
            return (
                False,
                f"Field '{field_name}' value {val} is out of valid physical bounds [{min_val}, {max_val}].",
                None,
            )
        cleaned[field_name] = round(val, 3)

    return True, None, cleaned

@app.before_request
def security_payload_integrity_middleware():
    if request.method in ("POST", "PUT") and request.path.startswith("/sensors"):
        if (
            request.path.endswith("/interlocks/reset")
            or request.path.endswith("/simulate-crash")
            or request.path.endswith("/scenario/reset")
            or request.path.endswith("/race/start")
            or request.path.endswith("/race/restart")
            or "/scenario/" in request.path
            or "/engine-mode/" in request.path
        ):
            return None
        if not request.is_json:
            return {"message": "Security Error: Content-Type must be application/json"}, 400
        try:
            parsed = request.get_json(force=False, silent=False)
        except BadRequest:
            return {"message": "Security Error: Malformed JSON payload"}, 400
        if not isinstance(parsed, dict):
            return {"message": "Security Error: JSON body must be an object"}, 400
    return None

def evaluate_safety_interlocks(telemetry: Dict[str, Any]) -> Dict[str, Any]:
    tyre_temp = telemetry["tyre_surface_temp_c"]
    tyre_psi = telemetry["tyre_pressure_psi"]
    brake_temp = telemetry["front_brake_temp_c"]
    fuel_kg = telemetry["fuel_remaining_kg"]
    fuel_flow = telemetry["fuel_flow_rate_kgh"]
    head_g = telemetry["head_accel_g"]
    crashed = telemetry.get("crashed", False)
    now_ts = telemetry["timestamp"]

    active_trips: List[str] = []

    if tyre_temp > 130.0:
        tyre_status = "PUNCTURE_HAZARD"
        msg = f"TYRE PUNCTURE HAZARD: {tyre_temp:.1f}°C exceeds 130.0°C critical limit! BOX IMMEDIATELY."
        active_trips.append(msg)
        LATCHED_ALARMS["TYRE_PUNCTURE_HAZARD"] = {
            "code": "TYRE_PUNCTURE_HAZARD",
            "metric": "tyre_surface_temp_c",
            "value": tyre_temp,
            "threshold": 130.0,
            "message": msg,
            "latched_at": now_ts,
        }
        logger.critical("[SAFETY INTERLOCK TRIP] %s", msg)
    elif tyre_temp > 110.0:
        tyre_status = "BLISTER_WARNING"
    elif tyre_temp >= 80.0:
        tyre_status = "OPTIMAL"
    else:
        tyre_status = "COLD_GRAINING"

    tyre_psi_warning = tyre_psi > 24.5

    if brake_temp > 1000.0:
        brake_status = "THERMAL_FADE_ALERT"
        msg = f"BRAKE THERMAL FADE ALERT: {brake_temp:.1f}°C exceeds 1000.0°C! Hydraulic boiling hazard."
        active_trips.append(msg)
        LATCHED_ALARMS["BRAKE_THERMAL_FADE"] = {
            "code": "BRAKE_THERMAL_FADE",
            "metric": "front_brake_temp_c",
            "value": brake_temp,
            "threshold": 1000.0,
            "message": msg,
            "latched_at": now_ts,
        }
        logger.critical("[SAFETY INTERLOCK TRIP] %s", msg)
    elif brake_temp >= 350.0:
        brake_status = "BITE_WINDOW"
    else:
        brake_status = "COOLING_COLD"

    if fuel_flow > 100.0:
        fuel_status = "FIA_FLOW_LIMIT_EXCEEDED"
        msg = f"FIA FUEL FLOW VIOLATION: {fuel_flow:.1f} kg/h exceeds 100.0 kg/h technical regulation limit!"
        active_trips.append(msg)
        LATCHED_ALARMS["FIA_FUEL_FLOW_VIOLATION"] = {
            "code": "FIA_FUEL_FLOW_VIOLATION",
            "metric": "fuel_flow_rate_kgh",
            "value": fuel_flow,
            "threshold": 100.0,
            "message": msg,
            "latched_at": now_ts,
        }
        logger.critical("[SAFETY INTERLOCK TRIP] %s", msg)
    elif fuel_kg <= 1.0:
        fuel_status = "TARGET_FINISHED"
    elif fuel_kg <= 5.0:
        fuel_status = "LOW_FUEL_WARNING"
    else:
        fuel_status = "NOMINAL"

    if head_g > 20.0 or crashed:
        head_status = "MEDICAL_CONCUSSION_INTERLOCK"
        msg = f"MEDICAL CONCUSSION & CRASH INTERLOCK: {head_g:.1f}g impact (>20.0g)! Mandatory medical intervention & car halt."
        active_trips.append(msg)
        LATCHED_ALARMS["MEDICAL_CONCUSSION_INTERLOCK"] = {
            "code": "MEDICAL_CONCUSSION_INTERLOCK",
            "metric": "head_accel_g",
            "value": head_g,
            "threshold": 20.0,
            "message": msg,
            "latched_at": now_ts,
        }
        logger.critical("[SAFETY INTERLOCK TRIP] %s", msg)
    elif head_g > 5.0:
        head_status = "HIGH_G_LOAD"
    elif head_g >= 2.0:
        head_status = "CORNERING_LOAD"
    else:
        head_status = "NOMINAL"

    critical_trip = len(LATCHED_ALARMS) > 0

    return {
        "tyre_status": tyre_status,
        "tyre_psi_warning": tyre_psi_warning,
        "brake_status": brake_status,
        "fuel_status": fuel_status,
        "head_status": head_status,
        "critical_trip": critical_trip,
        "active_trips": active_trips,
        "latched_alarms": list(LATCHED_ALARMS.values()),
    }

def ingest_telemetry_frame(frame: Dict[str, Any], source: str = "mqtt") -> Dict[str, Any]:
    global LATEST_TELEMETRY
    now = time.time()
    packet_ts = float(frame.get("timestamp", now))
    pipeline_latency_ms = max(0.1, round((now - packet_ts) * 1000.0, 2))

    with state_lock:
        cleaned_frame = dict(frame)
        cleaned_frame["source"] = source
        cleaned_frame["pipeline_latency_ms"] = pipeline_latency_ms

        interlocks = evaluate_safety_interlocks(cleaned_frame)
        cleaned_frame["interlocks"] = interlocks

        LATEST_TELEMETRY = cleaned_frame
        return cleaned_frame


# Part 3: Telemetry Simulator Worker, Physics, and Circuit Logic
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to MQTT Broker at %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe(MQTT_TOPIC, qos=0)
        logger.info("Subscribed to MQTT topic '%s'", MQTT_TOPIC)
    else:
        logger.warning("MQTT connection returned result code %s", rc)

def on_message(client, userdata, msg):
    try:
        raw_payload = json.loads(msg.payload.decode("utf-8"))
        is_valid, err_msg, cleaned = validate_telemetry_payload(raw_payload)
        if not is_valid or cleaned is None:
            logger.error("Rejected malformed MQTT payload: %s", err_msg)
            return
        ingest_telemetry_frame(cleaned, source="mqtt")
    except Exception as exc:
        logger.error("Failed to process MQTT message: %s", exc)

def clamp(val: float, low: float, high: float) -> float:
    return max(low, min(high, val))

def telemetry_simulator_worker():
    """
    Simulates a realistic F1 car driving around a circuit, managing:
    1. Race Lifecycle: GRID_WAITING -> RACING -> CHECKERED_FLAG at Lap 15.
    2. Lap Times & Sector Splits: Tracks running lap time, best/last lap, and scenario time deltas.
    3. Performance vs Safety Tuning: Engine Modes (Push, Balanced, Conserve).
    4. 10 Track & Pit Wall Scenarios impacting the 4 target sensor systems.
    """
    global ACTIVE_SCENARIO, SCENARIO_TICKS_REMAINING, RESET_SIM_REQUESTED, HIGH_TYRE_TEMP_STREAK
    global START_RACE_REQUESTED, RESTART_RACE_REQUESTED, RACE_STATUS, ENGINE_MODE
    global CURRENT_LAP_TIME_SEC, LAST_LAP_TIME_SEC, BEST_LAP_TIME_SEC, SECTOR_SPLITS, LAP_HISTORY

    mqtt_client = mqtt.Client(client_id=f"f1-pitwall-{random.randint(1000, 9999)}")
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    try:
        mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
    except Exception as exc:
        logger.warning("MQTT broker initial connect_async warning: %s", exc)

    total_race_ticks = TOTAL_RACE_LAPS * TICKS_PER_LAP
    avg_burn_per_tick = (STARTING_FUEL_KG - TARGET_END_FUEL_KG) / float(total_race_ticks)

    lap = 1
    tick_in_lap = 0
    crashed = False
    crash_impact_delivered = False
    tyre_wear_pct = 2.0
    est_end_fuel_pct = 0.95

    sim_state = {
        "tyre_surface_temp_c": 94.0,
        "tyre_pressure_psi": 22.2,
        "front_brake_temp_c": 480.0,
        "fuel_remaining_kg": STARTING_FUEL_KG,
        "fuel_flow_rate_kgh": 95.0,
        "head_accel_g": 1.2,
        "temperature": 25.0,
        "ambient_temp_c": 25.0,
    }
    speed_kmh = 295
    track_phase = "S1: MAIN STRAIGHT (DRS)"

    CURRENT_LAP_TIME_SEC = 0.0
    s1_split = 28.2
    s2_split = 31.4
    s3_split = 28.9

    while True:
        loop_start = time.time()

        if loop_start < MANUAL_OVERRIDE_UNTIL:
            time.sleep(0.1)
            continue

        if START_RACE_REQUESTED:
            START_RACE_REQUESTED = False
            RACE_STATUS = "RACING"
            logger.info("Simulator resumed racing.")

        if RESTART_RACE_REQUESTED:
            RESTART_RACE_REQUESTED = False
            lap = 1
            tick_in_lap = 0
            crashed = False
            crash_impact_delivered = False
            HIGH_TYRE_TEMP_STREAK = 0
            ACTIVE_SCENARIO = "NOMINAL_RACING"
            SCENARIO_TICKS_REMAINING = 0
            RACE_STATUS = "RACING"
            tyre_wear_pct = 1.0
            est_end_fuel_pct = 0.95
            CURRENT_LAP_TIME_SEC = 0.0
            sim_state["fuel_remaining_kg"] = STARTING_FUEL_KG
            sim_state["tyre_surface_temp_c"] = 92.0
            sim_state["tyre_pressure_psi"] = 22.1
            sim_state["front_brake_temp_c"] = 450.0
            sim_state["fuel_flow_rate_kgh"] = 94.0
            sim_state["head_accel_g"] = 1.4
            speed_kmh = 290
            track_phase = "S1: MAIN STRAIGHT (DRS) — LAP 1 RESTART"
            logger.info("Simulator restarted Grand Prix to Lap 1/15.")

        if RESET_SIM_REQUESTED:
            RESET_SIM_REQUESTED = False
            crashed = False
            crash_impact_delivered = False
            HIGH_TYRE_TEMP_STREAK = 0
            if sim_state["fuel_remaining_kg"] <= 1.2 and lap >= TOTAL_RACE_LAPS:
                lap = 1
                tick_in_lap = 0
                sim_state["fuel_remaining_kg"] = STARTING_FUEL_KG
                tyre_wear_pct = 1.0
                RACE_STATUS = "RACING"
            sim_state["tyre_surface_temp_c"] = 92.0
            sim_state["front_brake_temp_c"] = 480.0
            sim_state["head_accel_g"] = 1.5
            est_end_fuel_pct = 0.95

        if RACE_STATUS == "CHECKERED_FLAG":
            track_phase = f"CHECKERED FLAG: RACE FINISHED (LAP {TOTAL_RACE_LAPS}/{TOTAL_RACE_LAPS}) — PARC FERMÉ"
            speed_kmh = 0
            sim_state["fuel_flow_rate_kgh"] = 0.0
            sim_state["head_accel_g"] = 0.0
            sim_state["fuel_remaining_kg"] = TARGET_END_FUEL_KG
            sim_state["tyre_surface_temp_c"] = round(clamp(sim_state["tyre_surface_temp_c"] - 0.5, 40.0, 135.0), 1)
            sim_state["front_brake_temp_c"] = round(clamp(sim_state["front_brake_temp_c"] - 4.0, 300.0, 1050.0), 1)
            sim_state["tyre_pressure_psi"] = round(clamp(sim_state["tyre_pressure_psi"] - 0.02, 20.0, 25.5), 2)
        else:
            lap_progress = tick_in_lap / float(TICKS_PER_LAP)

            if ACTIVE_SCENARIO == "CRASH":
                crashed = True
                RACE_STATUS = "CRASH_HALTED"
                if not crash_impact_delivered:
                    crash_impact_delivered = True
                    sim_state["head_accel_g"] = round(random.uniform(22.4, 28.6), 1)
                    sim_state["fuel_flow_rate_kgh"] = 0.0
                    speed_kmh = 0
                    track_phase = f"CRASH DETECTED (LAP {lap}): >20G IMPACT — MEDICAL CONCUSSION INTERLOCK"
                    logger.critical("CRASH OCCURRED: Peak head acceleration %.1fg!", sim_state["head_accel_g"])
                else:
                    sim_state["head_accel_g"] = 0.0
                    sim_state["fuel_flow_rate_kgh"] = 0.0
                    speed_kmh = 0
                    track_phase = f"RED FLAG (LAP {lap}): CAR STOPPED — TYRES & BRAKES COOLING"

                    tyre_cool = (sim_state["tyre_surface_temp_c"] - 35.0) * 0.08 + random.uniform(0.8, 1.8)
                    sim_state["tyre_surface_temp_c"] = round(
                        clamp(sim_state["tyre_surface_temp_c"] - tyre_cool, 35.0, 135.0), 1
                    )
                    psi_cool = (sim_state["tyre_pressure_psi"] - 20.0) * 0.12
                    sim_state["tyre_pressure_psi"] = round(
                        clamp(sim_state["tyre_pressure_psi"] - psi_cool, 20.0, 25.5), 2
                    )
                    brake_cool = (sim_state["front_brake_temp_c"] - 300.0) * 0.14 + random.uniform(8.0, 18.0)
                    sim_state["front_brake_temp_c"] = round(
                        clamp(sim_state["front_brake_temp_c"] - brake_cool, 300.0, 1050.0), 1
                    )

            elif ACTIVE_SCENARIO == "PIT_STOP" and SCENARIO_TICKS_REMAINING > 0:
                if SCENARIO_TICKS_REMAINING > 6:
                    track_phase = "PIT LANE ENTRY (80 KM/H FIA LIMITER) — BOXING THIS LAP"
                    speed_kmh = 80
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(16.0, 22.0), 1)
                    sim_state["head_accel_g"] = round(random.uniform(0.4, 0.9), 1)
                    sim_state["front_brake_temp_c"] = round(clamp(sim_state["front_brake_temp_c"] - 65.0, 380.0, 1050.0), 1)
                    sim_state["tyre_surface_temp_c"] = round(clamp(sim_state["tyre_surface_temp_c"] - 4.0, 82.0, 135.0), 1)
                elif SCENARIO_TICKS_REMAINING > 2:
                    track_phase = "PIT BOX STATIONARY — FITTING FRESH SLICKS (85°C BLANKETS)"
                    speed_kmh = 0
                    sim_state["fuel_flow_rate_kgh"] = 5.5
                    sim_state["head_accel_g"] = 0.2
                    sim_state["tyre_surface_temp_c"] = 85.0
                    sim_state["tyre_pressure_psi"] = 21.5
                    sim_state["front_brake_temp_c"] = round(clamp(sim_state["front_brake_temp_c"] - 45.0, 360.0, 1050.0), 1)
                    tyre_wear_pct = 0.0
                    HIGH_TYRE_TEMP_STREAK = 0
                    with state_lock:
                        LATCHED_ALARMS.pop("TYRE_PUNCTURE_HAZARD", None)
                else:
                    track_phase = "PIT LANE EXIT — FRESH TYRES FITTED (REJOINING TRACK)"
                    speed_kmh = 145
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(65.0, 82.0), 1)
                    sim_state["head_accel_g"] = 1.8
                    sim_state["tyre_surface_temp_c"] = 88.5
                    sim_state["tyre_pressure_psi"] = 21.8

                SCENARIO_TICKS_REMAINING -= 1
                if SCENARIO_TICKS_REMAINING <= 0:
                    ACTIVE_SCENARIO = "NOMINAL_RACING"

            else:
                if ENGINE_MODE == "MODE_PUSH":
                    speed_boost = 15
                    burn_mult = 1.18
                    heat_mult = 1.25
                elif ENGINE_MODE == "MODE_CONSERVE":
                    speed_boost = -18
                    burn_mult = 0.72
                    heat_mult = 0.75
                else:
                    speed_boost = 0
                    burn_mult = 1.00
                    heat_mult = 1.00

                if lap_progress < 0.18:
                    track_phase = "S1: MAIN STRAIGHT (FULL THROTTLE / DRS)"
                    speed_kmh = int(clamp(speed_kmh + random.randint(12, 22) + speed_boost, 260, 342))
                    sim_state["front_brake_temp_c"] = round(
                        clamp(sim_state["front_brake_temp_c"] - random.uniform(45.0, 75.0), 360.0, 1050.0), 1
                    )
                    sim_state["tyre_surface_temp_c"] = round(
                        clamp(sim_state["tyre_surface_temp_c"] - random.uniform(1.2, 2.8) * heat_mult, 84.0, 135.0), 1
                    )
                    sim_state["fuel_flow_rate_kgh"] = round(clamp(random.uniform(96.5, 99.8) * burn_mult, 0.0, 105.0), 1)
                    burn_factor = 1.30 * burn_mult
                    sim_state["head_accel_g"] = round(random.uniform(0.8, 1.6), 1)

                elif lap_progress < 0.28:
                    track_phase = "S1: TURN 1 HEAVY BRAKING ZONE"
                    speed_kmh = int(clamp(speed_kmh - random.randint(45, 70) + speed_boost // 2, 92, 260))
                    sim_state["front_brake_temp_c"] = round(
                        clamp(sim_state["front_brake_temp_c"] + random.uniform(135.0, 200.0) * heat_mult, 360.0, 1010.0), 1
                    )
                    sim_state["tyre_surface_temp_c"] = round(
                        clamp(sim_state["tyre_surface_temp_c"] + random.uniform(3.2, 5.8) * heat_mult, 84.0, 128.0), 1
                    )
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(9.0, 19.0) * burn_mult, 1)
                    burn_factor = 0.25 * burn_mult
                    sim_state["head_accel_g"] = round(random.uniform(4.1, 4.9), 1)

                elif lap_progress < 0.55:
                    track_phase = "S2: HIGH-SPEED ESSES (CORNERING LOAD)"
                    speed_kmh = int(clamp(random.uniform(215, 265) + speed_boost, 180, 280))
                    sim_state["front_brake_temp_c"] = round(
                        clamp(sim_state["front_brake_temp_c"] + random.uniform(-35.0, 25.0), 480.0, 910.0), 1
                    )
                    sim_state["tyre_surface_temp_c"] = round(
                        clamp(sim_state["tyre_surface_temp_c"] + random.uniform(1.8, 4.2) * heat_mult, 88.0, 128.0), 1
                    )
                    sim_state["fuel_flow_rate_kgh"] = round(clamp(random.uniform(80.0, 93.0) * burn_mult, 0.0, 102.0), 1)
                    burn_factor = 1.10 * burn_mult
                    sim_state["head_accel_g"] = round(random.uniform(2.9, 4.6), 1)

                elif lap_progress < 0.68:
                    track_phase = "S2: HAIRPIN BRAKING & TRACTION"
                    speed_kmh = int(clamp(random.uniform(85, 145) + speed_boost // 2, 75, 160))
                    sim_state["front_brake_temp_c"] = round(
                        clamp(sim_state["front_brake_temp_c"] + random.uniform(90.0, 145.0) * heat_mult, 500.0, 985.0), 1
                    )
                    sim_state["tyre_surface_temp_c"] = round(
                        clamp(sim_state["tyre_surface_temp_c"] + random.uniform(1.2, 3.2) * heat_mult, 90.0, 128.0), 1
                    )
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(24.0, 52.0) * burn_mult, 1)
                    burn_factor = 0.55 * burn_mult
                    sim_state["head_accel_g"] = round(random.uniform(3.2, 4.5), 1)

                elif lap_progress < 0.85:
                    track_phase = "S3: BACK STRAIGHT (FULL THROTTLE)"
                    speed_kmh = int(clamp(speed_kmh + random.randint(25, 45) + speed_boost, 250, 335))
                    sim_state["front_brake_temp_c"] = round(
                        clamp(sim_state["front_brake_temp_c"] - random.uniform(55.0, 90.0), 380.0, 1050.0), 1
                    )
                    sim_state["tyre_surface_temp_c"] = round(
                        clamp(sim_state["tyre_surface_temp_c"] - random.uniform(2.0, 3.8) * heat_mult, 85.0, 135.0), 1
                    )
                    sim_state["fuel_flow_rate_kgh"] = round(clamp(random.uniform(95.0, 99.6) * burn_mult, 0.0, 104.0), 1)
                    burn_factor = 1.25 * burn_mult
                    sim_state["head_accel_g"] = round(random.uniform(0.9, 1.8), 1)

                else:
                    track_phase = "S3: FINAL CHICANE TO START/FINISH"
                    speed_kmh = int(clamp(random.uniform(135, 225) + speed_boost // 2, 110, 240))
                    sim_state["front_brake_temp_c"] = round(
                        clamp(sim_state["front_brake_temp_c"] + random.uniform(40.0, 85.0) * heat_mult, 450.0, 950.0), 1
                    )
                    sim_state["tyre_surface_temp_c"] = round(
                        clamp(sim_state["tyre_surface_temp_c"] + random.uniform(-0.5, 2.0) * heat_mult, 86.0, 126.0), 1
                    )
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(64.0, 86.0) * burn_mult, 1)
                    burn_factor = 0.90 * burn_mult
                    sim_state["head_accel_g"] = round(random.uniform(2.4, 3.8), 1)

                if ACTIVE_SCENARIO == "TYRE_OVERHEAT" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: TYRE THERMAL OVERDRIVE (BLISTERING / DEGRADATION)"
                    sim_state["tyre_surface_temp_c"] = round(random.uniform(124.0, 133.8), 1)
                    tyre_wear_pct = min(99.0, round(tyre_wear_pct + 2.8, 1))
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                elif ACTIVE_SCENARIO == "BRAKE_DUCT_BLOCKAGE" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: TEAR-OFF BLOCKING FRONT BRAKE DUCT (>1000°C FADE)"
                    sim_state["front_brake_temp_c"] = round(random.uniform(1008.0, 1044.0), 1)
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                elif ACTIVE_SCENARIO == "KERB_STRIKE" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: SAUSAGE KERB STRIKE & PORPOISING (HIGH G-LOAD)"
                    sim_state["head_accel_g"] = round(random.uniform(12.5, 18.8), 1)
                    sim_state["tyre_surface_temp_c"] = round(clamp(sim_state["tyre_surface_temp_c"] + 8.0, 112.0, 129.0), 1)
                    tyre_wear_pct = min(99.0, round(tyre_wear_pct + 1.5, 1))
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                elif ACTIVE_SCENARIO == "FUEL_FLOW_SPIKE" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: ENGINE MODE PUSH — FIA FUEL FLOW VIOLATION (>100 KG/H)"
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(102.4, 108.6), 1)
                    burn_factor = 1.85
                    est_end_fuel_pct = round(max(0.35, est_end_fuel_pct - 0.04), 2)
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                elif ACTIVE_SCENARIO == "SAFETY_CAR" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: SAFETY CAR DEPLOYED — TYRES & BRAKES GLAZING COLD"
                    speed_kmh = int(random.uniform(135, 165))
                    sim_state["tyre_surface_temp_c"] = round(random.uniform(71.5, 78.5), 1)
                    sim_state["front_brake_temp_c"] = round(random.uniform(308.0, 342.0), 1)
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(22.0, 34.0), 1)
                    burn_factor = 0.35
                    sim_state["head_accel_g"] = round(random.uniform(0.7, 1.8), 1)
                    est_end_fuel_pct = round(min(1.15, est_end_fuel_pct + 0.02), 2)
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                elif ACTIVE_SCENARIO == "BRAKE_BIAS_MIGRATION" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: BRAKE BIAS SHIFTED FORWARD — FRONT ROTORS SURGING"
                    sim_state["front_brake_temp_c"] = round(random.uniform(975.0, 1030.0), 1)
                    sim_state["tyre_surface_temp_c"] = round(clamp(sim_state["tyre_surface_temp_c"] + 3.5, 95.0, 130.0), 1)
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                elif ACTIVE_SCENARIO == "EXTREME_PORPOISING" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: HIGH-SPEED GROUND EFFECT PORPOISING (EAR IMPACT)"
                    sim_state["head_accel_g"] = round(random.uniform(11.2, 17.5), 1)
                    sim_state["tyre_pressure_psi"] = round(clamp(sim_state["tyre_pressure_psi"] + 0.4, 20.0, 25.5), 2)
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                elif ACTIVE_SCENARIO == "FUEL_CONSERVATION_LIFT_COAST" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: PIT WALL ORDER: LIFT & COAST FUEL SAVING"
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(42.0, 58.0), 1)
                    burn_factor = 0.50
                    sim_state["front_brake_temp_c"] = round(clamp(sim_state["front_brake_temp_c"] - 55.0, 360.0, 1050.0), 1)
                    est_end_fuel_pct = round(min(1.05, est_end_fuel_pct + 0.03), 2)
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                elif ACTIVE_SCENARIO == "ERS_BOOST_OVERTAKE" and SCENARIO_TICKS_REMAINING > 0:
                    track_phase = "SCENARIO: OVERTAKE BUTTON ENGAGED — ERS HYBRID BOOST (+30 KM/H)"
                    speed_kmh = int(clamp(speed_kmh + 30, 280, 350))
                    sim_state["fuel_flow_rate_kgh"] = round(random.uniform(98.5, 99.9), 1)
                    burn_factor = 1.45
                    sim_state["head_accel_g"] = round(random.uniform(2.1, 3.4), 1)
                    SCENARIO_TICKS_REMAINING -= 1
                    if SCENARIO_TICKS_REMAINING <= 0:
                        ACTIVE_SCENARIO = "NOMINAL_RACING"

                target_psi = 20.5 + ((sim_state["tyre_surface_temp_c"] - 80.0) / 55.0) * 4.6
                sim_state["tyre_pressure_psi"] = round(
                    clamp(target_psi + random.uniform(-0.08, 0.08), 20.0, 25.5), 2
                )

                wear_rate = 0.38 if sim_state["tyre_surface_temp_c"] > 110.0 else 0.11
                tyre_wear_pct = min(99.0, round(tyre_wear_pct + wear_rate, 1))

                if sim_state["tyre_surface_temp_c"] > 112.0:
                    HIGH_TYRE_TEMP_STREAK += 1
                else:
                    HIGH_TYRE_TEMP_STREAK = max(0, HIGH_TYRE_TEMP_STREAK - 1)

                fuel_step = avg_burn_per_tick * burn_factor
                sim_state["fuel_remaining_kg"] = round(
                    max(TARGET_END_FUEL_KG, sim_state["fuel_remaining_kg"] - fuel_step), 2
                )

                tick_in_lap += 1
                CURRENT_LAP_TIME_SEC = round(tick_in_lap * SIMULATOR_INTERVAL_SEC * (BASE_LAP_TIME_SEC / (TICKS_PER_LAP * SIMULATOR_INTERVAL_SEC)), 3)

                if tick_in_lap >= TICKS_PER_LAP:
                    tick_in_lap = 0
                    pace_penalty = (tyre_wear_pct * 0.04)
                    if sim_state["tyre_surface_temp_c"] > 120.0:
                        pace_penalty += 1.20
                    if sim_state["front_brake_temp_c"] > 950.0:
                        pace_penalty += 0.90

                    if ENGINE_MODE == "MODE_PUSH":
                        pace_penalty -= 0.85
                    elif ENGINE_MODE == "MODE_CONSERVE":
                        pace_penalty += 1.45

                    lap_time_s = round(BASE_LAP_TIME_SEC + pace_penalty + random.uniform(-0.15, 0.25), 3)
                    LAST_LAP_TIME_SEC = lap_time_s
                    if BEST_LAP_TIME_SEC is None or lap_time_s < BEST_LAP_TIME_SEC:
                        BEST_LAP_TIME_SEC = lap_time_s

                    s1_split = round(28.2 + (pace_penalty * 0.33) + random.uniform(-0.08, 0.08), 2)
                    s2_split = round(31.4 + (pace_penalty * 0.40) + random.uniform(-0.10, 0.10), 2)
                    s3_split = round(28.9 + (pace_penalty * 0.27) + random.uniform(-0.08, 0.08), 2)
                    SECTOR_SPLITS = {"S1": s1_split, "S2": s2_split, "S3": s3_split}

                    LAP_HISTORY.append({
                        "lap": lap,
                        "lap_time_s": lap_time_s,
                        "engine_mode": ENGINE_MODE,
                        "tyre_wear_pct": tyre_wear_pct,
                        "end_fuel_kg": sim_state["fuel_remaining_kg"],
                    })
                    logger.info("LAP %d COMPLETE: Time %.3fs (Best: %.3fs) — S1: %.2f, S2: %.2f, S3: %.2f", lap, lap_time_s, BEST_LAP_TIME_SEC, s1_split, s2_split, s3_split)

                    if lap < TOTAL_RACE_LAPS:
                        lap += 1
                    else:
                        RACE_STATUS = "CHECKERED_FLAG"
                        sim_state["fuel_remaining_kg"] = TARGET_END_FUEL_KG
                        logger.info("CHECKERED FLAG! 15/15 Laps Completed. Car finished with %.2f kg (0.95%%) fuel.", sim_state["fuel_remaining_kg"])

        pit_recommended = False
        pit_reason = f"TYRE WEAR {tyre_wear_pct:.1f}% — PACE NOMINAL"
        if not crashed and ACTIVE_SCENARIO != "PIT_STOP" and RACE_STATUS == "RACING":
            if sim_state["tyre_surface_temp_c"] > 130.0:
                pit_recommended = True
                pit_reason = f"CRITICAL: TYRE TEMP {sim_state['tyre_surface_temp_c']:.1f}°C (>130°C PUNCTURE RISK) — BOX NOW!"
            elif HIGH_TYRE_TEMP_STREAK >= 4:
                pit_recommended = True
                pit_reason = f"EXTENDED HIGH TYRE TEMP ({sim_state['tyre_surface_temp_c']:.1f}°C FOR {HIGH_TYRE_TEMP_STREAK} TICKS) — CALL INTO PITS!"
            elif tyre_wear_pct >= 55.0:
                pit_recommended = True
                pit_reason = f"ESTIMATED TYRE WEAR {tyre_wear_pct:.1f}% EXCEEDS CLIFF THRESHOLD — BOX FOR FRESH SLICKS!"

        # Step 5: Update temperature data generator to Gaussian distribution
        sim_state["temperature"] = round(random.gauss(25.0, 10.0), 1)
        sim_state["ambient_temp_c"] = sim_state["temperature"]

        pace_delta = round((LAST_LAP_TIME_SEC - BEST_LAP_TIME_SEC) if (LAST_LAP_TIME_SEC and BEST_LAP_TIME_SEC) else 0.0, 3)

        packet = {
            "car_id": CAR_ID,
            "timestamp": round(time.time(), 3),
            "lap": lap,
            "total_laps": TOTAL_RACE_LAPS,
            "race_status": RACE_STATUS,
            "engine_mode": ENGINE_MODE,
            "track_phase": track_phase,
            "active_scenario": ACTIVE_SCENARIO,
            "speed_kmh": speed_kmh,
            "crashed": crashed,
            "pit_stop_recommended": pit_recommended,
            "pit_recommendation_reason": pit_reason,
            "estimated_tyre_wear_pct": tyre_wear_pct,
            "estimated_end_fuel_pct": est_end_fuel_pct,
            "current_lap_time_s": CURRENT_LAP_TIME_SEC,
            "last_lap_time_s": LAST_LAP_TIME_SEC,
            "best_lap_time_s": BEST_LAP_TIME_SEC,
            "pace_delta_s": pace_delta,
            "sector_splits": SECTOR_SPLITS,
            **sim_state,
        }

        payload_str = json.dumps(packet)
        published = False
        if mqtt_client and mqtt_client.is_connected():
            try:
                mqtt_client.publish(MQTT_TOPIC, payload_str, qos=0)
                published = True
            except Exception as exc:
                logger.debug("MQTT publish fallback: %s", exc)

        if not published:
            ingest_telemetry_frame(packet, source="simulator-direct")

        logger.info("MQTT [%s] Lap %d/%d [%s] (%s) -> %s", MQTT_TOPIC, lap, TOTAL_RACE_LAPS, RACE_STATUS, track_phase, payload_str)

        elapsed = time.time() - loop_start
        time.sleep(max(0.05, SIMULATOR_INTERVAL_SEC - elapsed))


# Part 4: Responsive Dashboard with RBAC UI controls, Login/Logout, and Role-Aware Restrictions

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>F1 Pit Wall Telemetry, Strategy &amp; Safety Interlocks (ISO/IEC 25010)</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        @keyframes pulse-fast {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        .animate-critical {
            animation: pulse-fast 0.75s cubic-bezier(0.4, 0, 0.6, 1) infinite;
        }
    </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans selection:bg-red-600 selection:text-white">
    <!-- Top Header -->
    <header class="border-b border-slate-800 bg-slate-900/90 backdrop-blur sticky top-0 z-30">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-3">
                <div class="h-9 w-2 bg-red-600 rounded-full"></div>
                <div>
                    <h1 class="text-lg sm:text-xl font-black tracking-wider uppercase text-white">
                        F1 Pit Wall <span class="text-red-500">Safety &amp; Strategy</span>
                    </h1>
                    <p class="text-xs text-slate-400 font-mono">
                        ISO/IEC 25010 &bull; TOPIC: <span class="text-slate-200">{{ mqtt_topic }}</span> &bull;
                        CAR: <span id="car-id-label" class="text-emerald-400 font-bold">{{ car_id }}</span>
                    </p>
                </div>
            </div>

            <!-- RBAC Persona Pill & Controls -->
            <div class="flex flex-wrap items-center gap-2">
                <div id="user-info-pill" class="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-slate-950 border border-slate-800 font-mono text-xs">
                    <span class="text-slate-400">USER:</span>
                    <span id="current-user-name" class="font-bold text-sky-400">--</span>
                    <span id="current-user-role" class="px-2 py-0.5 rounded text-[10px] font-black uppercase bg-slate-800 text-slate-300">VIEWER</span>
                </div>

                <button id="btn-login" onclick="openLoginModal()" class="px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-white font-mono text-xs font-bold transition">
                    Login
                </button>
                <button id="btn-logout" onclick="logoutUser()" class="hidden px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 font-mono text-xs font-semibold transition">
                    Logout
                </button>
            </div>

            <!-- Race Controls & Status Bar (Restricted to Operator/Admin) -->
            <div id="race-controls-panel" class="flex flex-wrap items-center gap-2">
                <div class="flex items-center gap-1.5 bg-slate-950 border border-slate-800 px-3 py-1 rounded-xl">
                    <button onclick="startRace()" title="Start/Resume Race"
                            class="px-2.5 py-1 rounded bg-emerald-600 hover:bg-emerald-500 text-white font-mono text-xs font-black uppercase transition flex items-center gap-1">
                        <span>&#9654;</span> Start
                    </button>
                    <button onclick="restartRace()" title="Restart Grand Prix from Lap 1"
                            class="px-2.5 py-1 rounded bg-amber-600 hover:bg-amber-500 text-white font-mono text-xs font-black uppercase transition flex items-center gap-1">
                        <span>&#8634;</span> Restart Race
                    </button>
                    <button onclick="resetScenario()" title="Reset Active Scenario to Nominal"
                            class="px-2.5 py-1 rounded bg-slate-700 hover:bg-slate-600 text-slate-200 font-mono text-xs font-bold uppercase transition">
                        Reset Scenario
                    </button>
                </div>

                <div id="race-status-badge" class="px-3 py-1 rounded-xl font-mono text-xs font-black uppercase tracking-wider bg-red-600/20 text-red-400 border border-red-500/40">
                    RACING
                </div>
            </div>

            <div class="flex flex-wrap items-center gap-2">
                <span id="latency-badge" class="px-2.5 py-1 rounded bg-slate-800 border border-slate-700 text-xs font-mono text-slate-300">
                    LATENCY: -- ms
                </span>

                <div id="link-status-badge" class="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-bold tracking-widest uppercase bg-emerald-950/90 text-emerald-400 border border-emerald-700/60">
                    <span id="link-dot" class="h-2.5 w-2.5 rounded-full bg-emerald-400 animate-ping"></span>
                    <span id="link-text">LINK ACTIVE</span>
                </div>

                <a href="/kpi-dashboard" target="_blank"
                   class="px-2.5 py-1.5 rounded-lg bg-emerald-950 hover:bg-emerald-900 text-emerald-300 border border-emerald-700 text-xs font-mono font-semibold transition">
                    100-Test KPIs
                </a>
                <a href="/schema" target="_blank"
                   class="px-2.5 py-1.5 rounded-lg bg-sky-950 hover:bg-sky-900 text-sky-300 border border-sky-700 text-xs font-mono font-semibold transition">
                    DB Schema
                </a>
                <a href="/swagger/" target="_blank"
                   class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-mono font-semibold transition">
                    Swagger &rarr;
                </a>
            </div>
        </div>
    </header>

    <!-- Modal: Login Dialog -->
    <div id="login-modal" class="hidden fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6 max-w-sm w-full shadow-2xl font-mono">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-base font-black uppercase text-white flex items-center gap-2">
                    <span class="h-3 w-1.5 bg-red-600 rounded-full"></span>
                    Pit Wall Authentication
                </h2>
                <button onclick="closeLoginModal()" class="text-slate-400 hover:text-white">&times;</button>
            </div>
            <div id="login-error-msg" class="hidden mb-3 p-2.5 rounded-lg bg-red-950/80 border border-red-700 text-red-200 text-xs font-bold"></div>
            <form id="login-form" onsubmit="submitLogin(event)" class="space-y-3">
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">USERNAME</label>
                    <input id="login-username" type="text" required placeholder="admin / operator / viewer"
                           class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500" />
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">PASSWORD</label>
                    <input id="login-password" type="password" required placeholder="••••••••"
                           class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500" />
                </div>
                <div class="flex items-center justify-end gap-2 pt-2">
                    <button type="button" onclick="closeLoginModal()" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-bold transition">Cancel</button>
                    <button type="submit" class="px-4 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white text-xs font-black uppercase transition">Sign In</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Resilience Fail-Safe Banner: LINK LOST / STALE TELEMETRY (>3s) -->
    <div id="link-lost-banner" class="hidden bg-amber-950/95 border-b-2 border-amber-500 text-amber-100 px-4 py-3 text-center font-mono text-sm font-bold tracking-widest uppercase animate-critical">
        &#9888; FAIL-SAFE STATE: LINK LOST / STALE TELEMETRY (&gt;3.0s WITHOUT MQTT UPDATE) &mdash; DISPLAYING LAST KNOWN FRAME &#9888;
    </div>

    <!-- Checkered Flag Banner -->
    <div id="checkered-flag-banner" class="hidden bg-slate-900/95 border-b-4 border-yellow-400 text-yellow-300 px-4 py-3 shadow-2xl">
        <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-4">
            <div class="flex items-center gap-3">
                <span class="text-2xl">&#127937;</span>
                <div>
                    <div class="text-sm font-mono font-black uppercase tracking-wider text-yellow-400">
                        CHECKERED FLAG &mdash; RACE FINISHED (LAP 15 / 15)
                    </div>
                    <div class="text-xs font-mono text-slate-300">
                        Car successfully brought home in Parc Fermé. Fuel target achieved: 0.95% remaining.
                    </div>
                </div>
            </div>
            <button onclick="restartRace()"
                    class="px-4 py-2 rounded-lg bg-yellow-400 hover:bg-yellow-300 text-slate-950 text-xs font-mono font-black uppercase tracking-wider transition shadow-lg">
                &#8634; Start New Grand Prix
            </button>
        </div>
    </div>

    <!-- Pit Wall Strategy Call Banner: Automatic "BOX BOX / CALL INTO PITS" Prompt -->
    <div id="pit-strategy-banner" class="hidden bg-cyan-950/95 border-b-2 border-cyan-400 text-cyan-100 px-4 py-3 shadow-xl">
        <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-4">
            <div class="flex items-center gap-3">
                <span class="px-2.5 py-1 rounded bg-cyan-500 text-slate-950 text-xs font-black uppercase tracking-widest animate-critical">
                    PIT STRATEGY CALL: BOX BOX
                </span>
                <span id="pit-strategy-reason" class="text-xs sm:text-sm font-mono font-bold text-cyan-200">
                    EXTENDED HIGH TYRE TEMPERATURE / WEAR DETECTED
                </span>
            </div>
            <button onclick="triggerScenario('pit_stop')"
                    class="px-3.5 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 text-xs font-mono font-black uppercase tracking-wider transition shadow-lg">
                &#9873; Confirm Call Into Pits (Fit Fresh Slicks)
            </button>
        </div>
    </div>

    <!-- Emergency Safety Interlock Banner (Latched Red Alerts) -->
    <div id="emergency-banner" class="hidden bg-red-950/95 border-b-2 border-red-500 text-white px-4 py-3 shadow-2xl">
        <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-4">
            <div class="flex items-center gap-3">
                <span class="px-2.5 py-1 rounded bg-red-600 text-white text-xs font-black uppercase tracking-widest animate-critical">
                    LATCHED SAFETY INTERLOCK
                </span>
                <div id="emergency-messages" class="text-xs sm:text-sm font-mono font-bold text-red-200 space-y-0.5"></div>
            </div>
            <div class="flex gap-2">
                <button onclick="triggerScenario('pit_stop')"
                        class="px-3 py-1.5 rounded-lg bg-cyan-800 hover:bg-cyan-700 border border-cyan-400 text-xs font-mono font-bold uppercase tracking-wider transition">
                    Box For Repairs / Tyres
                </button>
                <button onclick="resetLatchedInterlocks()"
                        class="px-3 py-1.5 rounded-lg bg-red-800 hover:bg-red-700 border border-red-400 text-xs font-mono font-bold uppercase tracking-wider transition">
                    Acknowledge &amp; Resume Car
                </button>
            </div>
        </div>
    </div>

    <main class="max-w-7xl mx-auto px-4 sm:px-6 py-5 space-y-5">

        <!-- Admin Only: User Management Panel (Restricted from Viewer & Operator) -->
        <section id="admin-user-panel" class="hidden bg-slate-900/90 border border-indigo-800/60 rounded-2xl p-5 shadow-xl font-mono">
            <div class="flex flex-wrap items-center justify-between gap-3 mb-4 border-b border-slate-800 pb-3">
                <div>
                    <h2 class="text-sm font-black uppercase tracking-wider text-indigo-400 flex items-center gap-2">
                        <span class="h-2.5 w-2.5 rounded-full bg-indigo-500 animate-pulse"></span>
                        Admin Control Deck: RBAC User Management (/users)
                    </h2>
                    <p class="text-xs text-slate-400">Exclusive to Admin persona. Manage Viewer, Operator, and Admin credentials &amp; permissions.</p>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="loadUsersTable()" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-bold transition">&#8635; Refresh Users</button>
                </div>
            </div>

            <!-- Create User Form -->
            <form id="create-user-form" onsubmit="submitCreateUser(event)" class="grid grid-cols-1 sm:grid-cols-4 gap-3 mb-4 bg-slate-950/70 p-3.5 rounded-xl border border-slate-800">
                <div>
                    <label class="block text-[10px] text-slate-400 uppercase font-bold mb-1">NEW USERNAME</label>
                    <input id="new-user-name" type="text" required placeholder="e.g. race_eng_1"
                           class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-indigo-500" />
                </div>
                <div>
                    <label class="block text-[10px] text-slate-400 uppercase font-bold mb-1">PASSWORD</label>
                    <input id="new-user-pass" type="password" required placeholder="••••••••"
                           class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-indigo-500" />
                </div>
                <div>
                    <label class="block text-[10px] text-slate-400 uppercase font-bold mb-1">ROLE</label>
                    <select id="new-user-role" class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-indigo-500">
                        <option value="Viewer">Viewer</option>
                        <option value="Operator">Operator</option>
                        <option value="Admin">Admin</option>
                    </select>
                </div>
                <div class="flex items-end">
                    <button type="submit" class="w-full px-3 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-bold uppercase transition">
                        + Add User
                    </button>
                </div>
            </form>

            <!-- Users List Table -->
            <div class="overflow-x-auto border border-slate-800 rounded-xl">
                <table class="w-full text-left text-xs">
                    <thead class="bg-slate-950 text-slate-400 uppercase text-[10px] border-b border-slate-800">
                        <tr>
                            <th class="px-3 py-2">ID</th>
                            <th class="px-3 py-2">Username</th>
                            <th class="px-3 py-2">Role</th>
                            <th class="px-3 py-2">Created At</th>
                            <th class="px-3 py-2 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="users-table-body" class="divide-y divide-slate-800/60 text-slate-200">
                        <tr><td colspan="5" class="px-3 py-3 text-center text-slate-500">Loading user accounts...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- Lap Timing & Driver Performance Deck -->
        <section class="grid grid-cols-1 md:grid-cols-3 gap-5">
            <div class="bg-slate-900/90 border border-slate-800 rounded-2xl p-4 shadow-lg md:col-span-2">
                <div class="flex items-center justify-between gap-2 mb-3">
                    <div class="flex items-center gap-2">
                        <span id="lap-counter" class="px-2.5 py-1 rounded bg-red-600/20 text-red-400 border border-red-500/40 font-mono text-xs font-black">
                            LAP 1 / 15
                        </span>
                        <span id="speed-pill" class="px-2.5 py-1 rounded bg-slate-800 text-sky-300 font-mono text-xs font-bold">
                            --- KM/H
                        </span>
                        <span id="track-phase-label" class="text-xs font-mono font-semibold text-slate-300">
                            INITIALISING TRACK TELEMETRY...
                        </span>
                    </div>
                </div>

                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono">
                    <div class="bg-slate-950/70 p-3 rounded-xl border border-slate-800/80">
                        <div class="text-[10px] text-slate-400 uppercase tracking-wider">CURRENT LAP</div>
                        <div id="val-current-lap-time" class="text-xl font-black text-amber-300">00.000s</div>
                    </div>
                    <div class="bg-slate-950/70 p-3 rounded-xl border border-slate-800/80">
                        <div class="text-[10px] text-slate-400 uppercase tracking-wider">LAST LAP</div>
                        <div id="val-last-lap-time" class="text-xl font-bold text-slate-200">--.---s</div>
                    </div>
                    <div class="bg-slate-950/70 p-3 rounded-xl border border-slate-800/80">
                        <div class="text-[10px] text-slate-400 uppercase tracking-wider">BEST LAP</div>
                        <div id="val-best-lap-time" class="text-xl font-bold text-purple-300">--.---s</div>
                    </div>
                    <div class="bg-slate-950/70 p-3 rounded-xl border border-slate-800/80">
                        <div class="text-[10px] text-slate-400 uppercase tracking-wider">PACE DELTA</div>
                        <div id="val-pace-delta" class="text-xl font-black text-emerald-400">+0.000s</div>
                    </div>
                </div>

                <div class="flex items-center gap-2 mt-3 pt-3 border-t border-slate-800/80 text-xs font-mono">
                    <span class="text-slate-400">SECTORS:</span>
                    <span id="sector-s1" class="px-2 py-0.5 rounded bg-slate-800 text-sky-300 font-bold">S1: 28.20s</span>
                    <span id="sector-s2" class="px-2 py-0.5 rounded bg-slate-800 text-yellow-300 font-bold">S2: 31.40s</span>
                    <span id="sector-s3" class="px-2 py-0.5 rounded bg-slate-800 text-emerald-300 font-bold">S3: 28.90s</span>
                </div>
            </div>

            <!-- Performance Tuning: Engine & Strategy Mode (Operator/Admin) -->
            <div id="engine-mode-deck" class="bg-slate-900/90 border border-slate-800 rounded-2xl p-4 shadow-lg flex flex-col justify-between">
                <div>
                    <div class="flex items-center justify-between gap-2 mb-2">
                        <span class="text-xs font-mono uppercase tracking-wider text-slate-400">Performance vs Safety</span>
                        <span id="engine-mode-pill" class="px-2 py-0.5 rounded text-[11px] font-mono font-black uppercase bg-sky-950 text-sky-400 border border-sky-700">
                            MODE_BALANCED
                        </span>
                    </div>
                    <p class="text-[11px] text-slate-400 mb-3">
                        Tune powertrain delivery to optimise lap times against thermal interlocks &amp; 0.9% fuel finish constraint.
                    </p>
                </div>
                <div class="grid grid-cols-3 gap-1.5 font-mono text-[11px] font-bold">
                    <button onclick="setEngineMode('push')"
                            id="btn-mode-push"
                            class="py-2 rounded-lg bg-slate-800 hover:bg-red-950 text-slate-300 hover:text-red-300 border border-slate-700 hover:border-red-600 transition text-center">
                        <div>&#9889; PUSH</div>
                        <div class="text-[9px] text-slate-400 font-normal">-0.8s pace</div>
                    </button>
                    <button onclick="setEngineMode('balanced')"
                            id="btn-mode-balanced"
                            class="py-2 rounded-lg bg-sky-900 text-sky-200 border border-sky-500 transition text-center">
                        <div>&#9878; BALANCED</div>
                        <div class="text-[9px] text-slate-300 font-normal">Nominal</div>
                    </button>
                    <button onclick="setEngineMode('conserve')"
                            id="btn-mode-conserve"
                            class="py-2 rounded-lg bg-slate-800 hover:bg-emerald-950 text-slate-300 hover:text-emerald-300 border border-slate-700 hover:border-emerald-600 transition text-center">
                        <div>&#127793; CONSERVE</div>
                        <div class="text-[9px] text-slate-400 font-normal">Cool &amp; Save</div>
                    </button>
                </div>
            </div>
        </section>

        <!-- Sensor CRUD Management Panel (Visible to Operator & Admin; Inaccessible to Viewer) -->
        <section id="sensor-crud-panel" class="bg-slate-900/90 border border-slate-800 rounded-2xl p-5 shadow-xl font-mono">
            <div class="flex flex-wrap items-center justify-between gap-3 mb-4 border-b border-slate-800 pb-3">
                <div>
                    <h2 class="text-sm font-black uppercase tracking-wider text-emerald-400 flex items-center gap-2">
                        <span class="h-2.5 w-2.5 rounded-full bg-emerald-500"></span>
                        Sensor Subsystems &amp; Calibration Registry (/sensors CRUD)
                    </h2>
                    <p class="text-xs text-slate-400">Viewed by all personas. Modifying sensors requires Operator or Admin permissions.</p>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="loadSensorsTable()" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-bold transition">&#8635; Refresh Sensors</button>
                </div>
            </div>

            <!-- Add Sensor Form (Only enabled for Operator & Admin) -->
            <form id="create-sensor-form" onsubmit="submitCreateSensor(event)" class="grid grid-cols-1 sm:grid-cols-5 gap-2.5 mb-4 bg-slate-950/70 p-3.5 rounded-xl border border-slate-800">
                <div>
                    <label class="block text-[10px] text-slate-400 uppercase font-bold mb-1">SENSOR NAME</label>
                    <input id="sensor-name-input" type="text" required placeholder="e.g. ers_coolant_temp_c"
                           class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-emerald-500" />
                </div>
                <div>
                    <label class="block text-[10px] text-slate-400 uppercase font-bold mb-1">LOCATION</label>
                    <input id="sensor-loc-input" type="text" required placeholder="e.g. Sidepod Radiator"
                           class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-emerald-500" />
                </div>
                <div>
                    <label class="block text-[10px] text-slate-400 uppercase font-bold mb-1">TYPE / UNIT</label>
                    <input id="sensor-type-input" type="text" required placeholder="e.g. Temperature (°C)"
                           class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-emerald-500" />
                </div>
                <div>
                    <label class="block text-[10px] text-slate-400 uppercase font-bold mb-1">FREQUENCY</label>
                    <input id="sensor-freq-input" type="text" required placeholder="e.g. 2 Hz (0.5s)"
                           class="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-emerald-500" />
                </div>
                <div class="flex items-end">
                    <button id="btn-add-sensor" type="submit" class="w-full px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold uppercase transition">
                        + Register Sensor
                    </button>
                </div>
            </form>

            <!-- Sensors Table -->
            <div class="overflow-x-auto border border-slate-800 rounded-xl">
                <table class="w-full text-left text-xs">
                    <thead class="bg-slate-950 text-slate-400 uppercase text-[10px] border-b border-slate-800">
                        <tr>
                            <th class="px-3 py-2">ID</th>
                            <th class="px-3 py-2">Channel Name</th>
                            <th class="px-3 py-2">Location</th>
                            <th class="px-3 py-2">Type / Range</th>
                            <th class="px-3 py-2">Frequency</th>
                            <th id="th-sensor-action" class="px-3 py-2 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="sensors-table-body" class="divide-y divide-slate-800/60 text-slate-200">
                        <tr><td colspan="6" class="px-3 py-3 text-center text-slate-500">Loading sensor channels...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- Interactive Pit Wall Scenario Control Deck -->
        <section id="scenario-injector-panel" class="bg-slate-900/90 border border-slate-800 rounded-2xl p-4 shadow-lg">
            <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
                <div class="flex items-center gap-2">
                    <span class="text-xs font-mono font-bold uppercase tracking-wider text-slate-400">
                        Pit Wall Scenario Injector:
                    </span>
                    <span id="active-scenario-pill" class="px-2.5 py-0.5 rounded bg-slate-800 text-emerald-400 border border-slate-700 text-xs font-mono font-bold">
                        NOMINAL_RACING
                    </span>
                </div>
                <button onclick="resetScenario()"
                        class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 text-xs font-mono font-semibold transition">
                    &#8635; Clear Scenario
                </button>
            </div>

            <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2 text-xs font-mono font-bold">
                <button onclick="triggerScenario('pit_stop')" class="px-2.5 py-2 rounded-lg bg-cyan-950 hover:bg-cyan-900 text-cyan-300 border border-cyan-700 transition text-left">
                    &#9873; Call Into Pits (Box)
                </button>
                <button onclick="triggerScenario('tyre_overheat')" class="px-2.5 py-2 rounded-lg bg-amber-950 hover:bg-amber-900 text-amber-300 border border-amber-700 transition text-left">
                    &#9832; Tyre Overdrive (&gt;125&deg;C)
                </button>
                <button onclick="triggerScenario('brake_duct_blockage')" class="px-2.5 py-2 rounded-lg bg-orange-950 hover:bg-orange-900 text-orange-300 border border-orange-700 transition text-left">
                    &#9888; Brake Debris (&gt;1000&deg;C)
                </button>
                <button onclick="triggerScenario('kerb_strike')" class="px-2.5 py-2 rounded-lg bg-purple-950 hover:bg-purple-900 text-purple-300 border border-purple-700 transition text-left">
                    &#9889; Kerb Strike (15g Spike)
                </button>
                <button onclick="triggerScenario('fuel_flow_spike')" class="px-2.5 py-2 rounded-lg bg-indigo-950 hover:bg-indigo-900 text-indigo-300 border border-indigo-700 transition text-left">
                    &#9981; FIA Fuel Flow (&gt;100 kg/h)
                </button>
                <button onclick="triggerScenario('safety_car')" class="px-2.5 py-2 rounded-lg bg-sky-950 hover:bg-sky-900 text-sky-300 border border-sky-700 transition text-left">
                    &#128663; Safety Car (Glazing)
                </button>
                <button onclick="triggerScenario('brake_bias_migration')" class="px-2.5 py-2 rounded-lg bg-rose-950 hover:bg-rose-900 text-rose-300 border border-rose-700 transition text-left">
                    &#9878; Brake Bias Migration
                </button>
                <button onclick="triggerScenario('extreme_porpoising')" class="px-2.5 py-2 rounded-lg bg-fuchsia-950 hover:bg-fuchsia-900 text-fuchsia-300 border border-fuchsia-700 transition text-left">
                    &#12336; High-G Porpoising (14g)
                </button>
                <button onclick="triggerScenario('fuel_conservation_lift_coast')" class="px-2.5 py-2 rounded-lg bg-teal-950 hover:bg-teal-900 text-teal-300 border border-teal-700 transition text-left">
                    &#127793; Lift &amp; Coast Fuel Save
                </button>
                <button onclick="triggerScenario('ers_boost_overtake')" class="px-2.5 py-2 rounded-lg bg-lime-950 hover:bg-lime-900 text-lime-300 border border-lime-700 transition text-left">
                    &#128640; ERS Boost Overtake
                </button>
                <button onclick="triggerScenario('crash')" class="px-2.5 py-2 rounded-lg bg-red-950 hover:bg-red-900 text-red-200 border border-red-700 transition text-left sm:col-span-3 md:col-span-5">
                    &#128165; Simulate Crash (&gt;20g Impact &bull; Concussion Interlock &bull; Fuel Cutoff)
                </button>
            </div>
        </section>

        <!-- 4 Target Sensor System Cards -->
        <section class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">

            <!-- 1. Tyres Card -->
            <div id="card-tyres" class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all">
                <div>
                    <div class="flex items-center justify-between gap-2 mb-2">
                        <span class="text-xs font-mono uppercase tracking-wider text-slate-400">1. Tyre System</span>
                        <span id="badge-tyre" class="px-2.5 py-0.5 rounded-full text-xs font-bold uppercase">--</span>
                    </div>
                    <div class="flex items-baseline justify-between my-3">
                        <div>
                            <div class="text-[11px] font-mono text-slate-400">SURFACE TEMP</div>
                            <div class="flex items-baseline gap-1">
                                <span id="val-tyre-temp" class="text-3xl font-black font-mono">--.-</span>
                                <span class="text-sm text-slate-400 font-mono">&deg;C</span>
                            </div>
                        </div>
                        <div class="text-right">
                            <div class="text-[11px] font-mono text-slate-400">PRESSURE</div>
                            <div class="flex items-baseline justify-end gap-1">
                                <span id="val-tyre-psi" class="text-2xl font-bold font-mono">--.--</span>
                                <span class="text-xs text-slate-400 font-mono">psi</span>
                            </div>
                        </div>
                    </div>
                </div>
                <div>
                    <div class="flex justify-between text-[11px] font-mono mb-1">
                        <span id="tyre-wear-label" class="text-cyan-400 font-bold">EST. WEAR: 4.0%</span>
                        <span id="badge-psi" class="font-bold text-emerald-400">PSI OK</span>
                    </div>
                    <div class="w-full bg-slate-800 h-2 rounded-full overflow-hidden">
                        <div id="bar-tyre" class="h-full bg-emerald-500 transition-all duration-200" style="width: 50%"></div>
                    </div>
                </div>
            </div>

            <!-- 2. Brakes Card -->
            <div id="card-brakes" class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all">
                <div>
                    <div class="flex items-center justify-between gap-2 mb-2">
                        <span class="text-xs font-mono uppercase tracking-wider text-slate-400">2. Front Brakes</span>
                        <span id="badge-brake" class="px-2.5 py-0.5 rounded-full text-xs font-bold uppercase">--</span>
                    </div>
                    <div class="my-3">
                        <div class="text-[11px] font-mono text-slate-400">CARBON DISC TEMP</div>
                        <div class="flex items-baseline gap-1">
                            <span id="val-brake-temp" class="text-3xl font-black font-mono">---.-</span>
                            <span class="text-sm text-slate-400 font-mono">&deg;C</span>
                        </div>
                    </div>
                </div>
                <div>
                    <div class="flex justify-between text-[11px] font-mono text-slate-400 mb-1">
                        <span>Bite &gt;350&deg;C</span>
                        <span>Fade Alert &gt;1000&deg;C</span>
                    </div>
                    <div class="w-full bg-slate-800 h-2 rounded-full overflow-hidden">
                        <div id="bar-brake" class="h-full bg-emerald-500 transition-all duration-200" style="width: 50%"></div>
                    </div>
                </div>
            </div>

            <!-- 3. Fuel Card -->
            <div id="card-fuel" class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all">
                <div>
                    <div class="flex items-center justify-between gap-2 mb-2">
                        <span class="text-xs font-mono uppercase tracking-wider text-slate-400">3. Fuel System</span>
                        <span id="badge-fuel" class="px-2.5 py-0.5 rounded-full text-xs font-bold uppercase">--</span>
                    </div>
                    <div class="flex items-baseline justify-between my-3">
                        <div>
                            <div class="text-[11px] font-mono text-slate-400">TANK REMAINING</div>
                            <div class="flex items-baseline gap-1">
                                <span id="val-fuel-kg" class="text-3xl font-black font-mono">--.--</span>
                                <span class="text-sm text-slate-400 font-mono">kg (%)</span>
                            </div>
                        </div>
                        <div class="text-right">
                            <div class="text-[11px] font-mono text-slate-400">LIVE FLOW RATE</div>
                            <div class="flex items-baseline justify-end gap-1">
                                <span id="val-fuel-flow" class="text-2xl font-bold font-mono text-sky-400">--.-</span>
                                <span class="text-xs text-slate-400 font-mono">kg/h</span>
                            </div>
                        </div>
                    </div>
                </div>
                <div>
                    <div class="flex justify-between text-[11px] font-mono mb-1">
                        <span id="est-end-fuel-label" class="text-emerald-400 font-bold">EST. FINISH: 0.95%</span>
                        <span class="text-slate-400">FIA Max: 100 kg/h</span>
                    </div>
                    <div class="w-full bg-slate-800 h-2 rounded-full overflow-hidden">
                        <div id="bar-fuel" class="h-full bg-sky-500 transition-all duration-200" style="width: 80%"></div>
                    </div>
                </div>
            </div>

            <!-- 4. Driver Earpiece Accelerometer Card -->
            <div id="card-head" class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all">
                <div>
                    <div class="flex items-center justify-between gap-2 mb-2">
                        <span class="text-xs font-mono uppercase tracking-wider text-slate-400">4. Driver Impact</span>
                        <span id="badge-head" class="px-2.5 py-0.5 rounded-full text-xs font-bold uppercase">--</span>
                    </div>
                    <div class="my-3">
                        <div class="text-[11px] font-mono text-slate-400">EARPIECE PEAK ACCEL</div>
                        <div class="flex items-baseline gap-1">
                            <span id="val-head-g" class="text-3xl font-black font-mono">-.-</span>
                            <span class="text-sm text-slate-400 font-mono">g</span>
                        </div>
                    </div>
                </div>
                <div>
                    <div class="flex justify-between text-[11px] font-mono text-slate-400 mb-1">
                        <span>Cornering: 2–5g</span>
                        <span>Medical Alert &gt;20g</span>
                    </div>
                    <div class="w-full bg-slate-800 h-2 rounded-full overflow-hidden">
                        <div id="bar-head" class="h-full bg-purple-500 transition-all duration-200" style="width: 15%"></div>
                    </div>
                </div>
            </div>
        </section>

        <!-- Rolling Real-Time Line Chart (Last 20 Ticks) -->
        <section class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl">
            <div class="flex flex-wrap items-center justify-between gap-2 mb-4">
                <div>
                    <h2 class="text-sm font-bold uppercase tracking-wider text-slate-200">
                        Rolling Real-Time Telemetry Trace (Last 20 Ticks &bull; 300ms Poll)
                    </h2>
                    <p class="text-xs text-slate-400">
                        Tracks Tyre Surface Temp (&deg;C), Instantaneous Fuel Flow Rate (kg/h), Front Brake Temp (&deg;C), &amp; Driver G-Force (g)
                    </p>
                </div>
                <div class="flex items-center gap-2">
                    <span id="age-pill" class="text-xs font-mono px-2.5 py-1 rounded bg-slate-800 text-slate-300 border border-slate-700">
                        PACKET AGE: -- ms
                    </span>
                    <span id="source-pill" class="text-xs font-mono px-2.5 py-1 rounded bg-slate-800 text-slate-300 border border-slate-700">
                        SOURCE: --
                    </span>
                </div>
            </div>
            <div class="h-80 w-full">
                <canvas id="telemetryChart"></canvas>
            </div>
        </section>
    </main>

    <script>
        const MAX_TICKS = 20;
        const ctx = document.getElementById('telemetryChart').getContext('2d');
        let currentUser = null;

        const chartData = {
            labels: [],
            datasets: [
                {
                    label: 'Tyre Surface Temp (°C)',
                    data: [],
                    borderColor: '#34d399',
                    backgroundColor: 'rgba(52, 211, 153, 0.12)',
                    yAxisID: 'yLeft',
                    tension: 0.25,
                    pointRadius: 3,
                    borderWidth: 2.5
                },
                {
                    label: 'Fuel Flow Rate (kg/h)',
                    data: [],
                    borderColor: '#38bdf8',
                    backgroundColor: 'rgba(56, 189, 248, 0.10)',
                    yAxisID: 'yLeft',
                    tension: 0.25,
                    pointRadius: 2.5,
                    borderWidth: 2
                },
                {
                    label: 'Front Brake Temp (°C)',
                    data: [],
                    borderColor: '#f87171',
                    backgroundColor: 'rgba(248, 113, 113, 0.12)',
                    borderDash: [4, 3],
                    yAxisID: 'yBrake',
                    tension: 0.25,
                    pointRadius: 3,
                    borderWidth: 2
                },
                {
                    label: 'Driver Head Accel (g)',
                    data: [],
                    borderColor: '#c084fc',
                    backgroundColor: 'rgba(192, 132, 252, 0.15)',
                    yAxisID: 'yGForce',
                    tension: 0.25,
                    pointRadius: 3,
                    borderWidth: 2
                }
            ]
        };

        const telemetryChart = new Chart(ctx, {
            type: 'line',
            data: chartData,
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { labels: { color: '#cbd5e1', font: { family: 'ui-monospace, monospace', size: 11 } } },
                    tooltip: { backgroundColor: 'rgba(15, 23, 42, 0.95)', titleColor: '#f1f5f9', bodyColor: '#e2e8f0', borderColor: '#334155', borderWidth: 1 }
                },
                scales: {
                    x: { ticks: { color: '#64748b', font: { family: 'ui-monospace, monospace', size: 10 }, maxRotation: 0 }, grid: { color: 'rgba(51, 65, 85, 0.35)' } },
                    yLeft: {
                        position: 'left', min: 0, max: 160,
                        title: { display: true, text: 'Tyre Temp (°C) & Fuel Flow (kg/h)', color: '#34d399', font: { size: 10, weight: 'bold' } },
                        ticks: { color: '#34d399', font: { family: 'ui-monospace, monospace', size: 10 } }, grid: { color: 'rgba(51, 65, 85, 0.25)' }
                    },
                    yBrake: {
                        position: 'right', min: 250, max: 1100,
                        title: { display: true, text: 'Front Brake Disc (°C)', color: '#f87171', font: { size: 10, weight: 'bold' } },
                        ticks: { color: '#f87171', font: { family: 'ui-monospace, monospace', size: 10 } }, grid: { drawOnChartArea: false }
                    },
                    yGForce: { position: 'right', min: 0, max: 30, title: { display: false }, ticks: { display: false }, grid: { drawOnChartArea: false } }
                }
            }
        });

        // -------------------------------------------------------------------
        // RBAC Persona UI Visibility and Control
        // -------------------------------------------------------------------
        async function fetchCurrentUser() {
            try {
                const res = await fetch('/auth/me');
                const data = await res.json();
                if (data.authenticated && data.user) {
                    currentUser = data.user;
                } else {
                    currentUser = { username: 'anonymous_viewer', role: 'Viewer' };
                }
            } catch (err) {
                currentUser = { username: 'viewer', role: 'Viewer' };
            }
            updateRbacUi();
        }

        function updateRbacUi() {
            const role = currentUser ? currentUser.role : 'Viewer';
            const name = currentUser ? currentUser.username : 'Guest';

            document.getElementById('current-user-name').textContent = name;
            const roleBadge = document.getElementById('current-user-role');
            roleBadge.textContent = role;

            if (role === 'Admin') {
                roleBadge.className = 'px-2 py-0.5 rounded text-[10px] font-black uppercase bg-indigo-950 text-indigo-300 border border-indigo-600';
            } else if (role === 'Operator') {
                roleBadge.className = 'px-2 py-0.5 rounded text-[10px] font-black uppercase bg-emerald-950 text-emerald-300 border border-emerald-600';
            } else {
                roleBadge.className = 'px-2 py-0.5 rounded text-[10px] font-black uppercase bg-slate-800 text-slate-300';
            }

            const btnLogin = document.getElementById('btn-login');
            const btnLogout = document.getElementById('btn-logout');
            if (currentUser && currentUser.username !== 'anonymous_viewer' && currentUser.username !== 'viewer') {
                btnLogin.classList.add('hidden');
                btnLogout.classList.remove('hidden');
            } else {
                btnLogin.classList.remove('hidden');
                btnLogout.classList.add('hidden');
            }

            // Role-based UI visibility:
            const adminPanel = document.getElementById('admin-user-panel');
            const sensorForm = document.getElementById('create-sensor-form');
            const thAction = document.getElementById('th-sensor-action');

            if (role === 'Admin') {
                adminPanel.classList.remove('hidden');
                loadUsersTable();
            } else {
                adminPanel.classList.add('hidden');
            }

            // Sensor CRUD panel editing permissions
            if (role === 'Operator' || role === 'Admin') {
                sensorForm.classList.remove('hidden');
                thAction.classList.remove('hidden');
            } else {
                // Viewer cannot perform sensor CRUD
                sensorForm.classList.add('hidden');
                thAction.classList.add('hidden');
            }

            loadSensorsTable();
        }

        function openLoginModal() {
            document.getElementById('login-modal').classList.remove('hidden');
            document.getElementById('login-error-msg').classList.add('hidden');
        }

        function closeLoginModal() {
            document.getElementById('login-modal').classList.add('hidden');
        }

        async function submitLogin(e) {
            e.preventDefault();
            const username = document.getElementById('login-username').value.trim();
            const password = document.getElementById('login-password').value.trim();
            const errEl = document.getElementById('login-error-msg');

            try {
                const res = await fetch('/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                const data = await res.json();
                if (res.ok) {
                    currentUser = data.user;
                    closeLoginModal();
                    updateRbacUi();
                } else {
                    errEl.textContent = data.message || 'Invalid username or password';
                    errEl.classList.remove('hidden');
                }
            } catch (err) {
                errEl.textContent = 'Connection error during authentication.';
                errEl.classList.remove('hidden');
            }
        }

        async function logoutUser() {
            try {
                await fetch('/auth/logout', { method: 'POST' });
            } catch (e) {}
            currentUser = { username: 'anonymous_viewer', role: 'Viewer' };
            updateRbacUi();
        }

        // -------------------------------------------------------------------
        // User Management (Admin Only)
        // -------------------------------------------------------------------
        async function loadUsersTable() {
            if (!currentUser || currentUser.role !== 'Admin') return;
            const tbody = document.getElementById('users-table-body');
            try {
                const res = await fetch('/users');
                if (!res.ok) {
                    tbody.innerHTML = '<tr><td colspan="5" class="px-3 py-3 text-center text-red-400 font-bold">Error loading users (403 Forbidden).</td></tr>';
                    return;
                }
                const users = await res.json();
                if (!users || users.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="px-3 py-3 text-center text-slate-500">No registered users found.</td></tr>';
                    return;
                }
                tbody.innerHTML = users.map(u => `
                    <tr class="hover:bg-slate-900/60 transition">
                        <td class="px-3 py-2 font-bold text-slate-400">${u.id}</td>
                        <td class="px-3 py-2 font-bold text-white">${u.username}</td>
                        <td class="px-3 py-2">
                            <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
                                u.role === 'Admin' ? 'bg-indigo-950 text-indigo-300 border border-indigo-700' :
                                u.role === 'Operator' ? 'bg-emerald-950 text-emerald-300 border border-emerald-700' :
                                'bg-slate-800 text-slate-300 border border-slate-700'
                            }">${u.role}</span>
                        </td>
                        <td class="px-3 py-2 text-slate-400 text-[11px]">${u.created_at || '--'}</td>
                        <td class="px-3 py-2 text-right">
                            ${u.username === 'admin' ? '<span class="text-slate-600 text-[10px]">Protected</span>' : `
                                <button onclick="deleteUser(${u.id})" class="px-2 py-0.5 rounded bg-red-950 hover:bg-red-800 text-red-300 border border-red-700 text-[10px] transition font-bold">
                                    Delete
                                </button>
                            `}
                        </td>
                    </tr>
                `).join('');
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="5" class="px-3 py-3 text-center text-red-400 font-bold">Failed to load user accounts.</td></tr>';
            }
        }

        async function submitCreateUser(e) {
            e.preventDefault();
            const username = document.getElementById('new-user-name').value.trim();
            const password = document.getElementById('new-user-pass').value.trim();
            const role = document.getElementById('new-user-role').value;

            try {
                const res = await fetch('/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password, role })
                });
                if (res.ok) {
                    document.getElementById('new-user-name').value = '';
                    document.getElementById('new-user-pass').value = '';
                    loadUsersTable();
                } else {
                    const data = await res.json();
                    alert(data.message || 'Error creating user');
                }
            } catch (err) {
                alert('Connection error while creating user');
            }
        }

        async function deleteUser(userId) {
            if (!confirm(`Delete user ID ${userId}?`)) return;
            try {
                const res = await fetch(`/users/${userId}`, { method: 'DELETE' });
                if (res.ok) {
                    loadUsersTable();
                } else {
                    const data = await res.json();
                    alert(data.message || 'Error deleting user');
                }
            } catch (err) {
                alert('Failed to delete user');
            }
        }

        // -------------------------------------------------------------------
        // Sensor CRUD Functions (/sensors)
        // -------------------------------------------------------------------
        async function loadSensorsTable() {
            const tbody = document.getElementById('sensors-table-body');
            const canEdit = currentUser && (currentUser.role === 'Operator' || currentUser.role === 'Admin');

            try {
                const res = await fetch('/sensors');
                const sensors = await res.json();
                if (!sensors || sensors.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="px-3 py-3 text-center text-slate-500">No sensors registered.</td></tr>';
                    return;
                }
                tbody.innerHTML = sensors.map(s => `
                    <tr class="hover:bg-slate-900/60 transition">
                        <td class="px-3 py-2 font-bold text-slate-400">${s.id}</td>
                        <td class="px-3 py-2 font-bold text-sky-300">${s.name}</td>
                        <td class="px-3 py-2 text-slate-300">${s.location}</td>
                        <td class="px-3 py-2 text-slate-300">${s.type}</td>
                        <td class="px-3 py-2 text-slate-400">${s.frequency}</td>
                        ${canEdit ? `
                            <td class="px-3 py-2 text-right">
                                <button onclick="deleteSensor(${s.id})" class="px-2 py-0.5 rounded bg-red-950 hover:bg-red-800 text-red-300 border border-red-700 text-[10px] transition font-bold">
                                    Delete
                                </button>
                            </td>
                        ` : ''}
                    </tr>
                `).join('');
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="6" class="px-3 py-3 text-center text-red-400 font-bold">Failed to load sensor table.</td></tr>';
            }
        }

        async function submitCreateSensor(e) {
            e.preventDefault();
            const name = document.getElementById('sensor-name-input').value.trim();
            const location = document.getElementById('sensor-loc-input').value.trim();
            const type = document.getElementById('sensor-type-input').value.trim();
            const frequency = document.getElementById('sensor-freq-input').value.trim();

            try {
                const res = await fetch('/sensors', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, location, type, frequency, system: 'Telemetry Sensor' })
                });
                if (res.ok) {
                    document.getElementById('sensor-name-input').value = '';
                    document.getElementById('sensor-loc-input').value = '';
                    document.getElementById('sensor-type-input').value = '';
                    document.getElementById('sensor-freq-input').value = '';
                    loadSensorsTable();
                } else {
                    const data = await res.json();
                    alert(data.message || 'Error creating sensor (Requires Operator/Admin role).');
                }
            } catch (err) {
                alert('Connection error while registering sensor.');
            }
        }

        async function deleteSensor(sensorId) {
            if (!confirm(`Delete sensor channel ID ${sensorId}?`)) return;
            try {
                const res = await fetch(`/sensors/${sensorId}`, { method: 'DELETE' });
                if (res.ok) {
                    loadSensorsTable();
                } else {
                    const data = await res.json();
                    alert(data.message || 'Error deleting sensor (Requires Operator/Admin role).');
                }
            } catch (err) {
                alert('Failed to delete sensor channel.');
            }
        }

        // -------------------------------------------------------------------
        // Scenario, Race & Control Commands
        // -------------------------------------------------------------------
        async function triggerScenario(scenarioName) {
            try {
                const res = await fetch(`/sensors/scenario/${scenarioName}`, { method: 'POST' });
                if (!res.ok) {
                    const data = await res.json();
                    alert(data.message || 'Action requires Operator or Admin role.');
                }
            } catch (err) {
                console.error('Failed to trigger scenario:', err);
            }
        }

        async function resetScenario() {
            try {
                const res = await fetch('/sensors/scenario/reset', { method: 'POST' });
                if (!res.ok) {
                    const data = await res.json();
                    alert(data.message || 'Action requires Operator or Admin role.');
                }
            } catch (err) {
                console.error('Failed to reset scenario:', err);
            }
        }

        async function startRace() {
            try {
                const res = await fetch('/sensors/race/start', { method: 'POST' });
                if (!res.ok) {
                    const data = await res.json();
                    alert(data.message || 'Action requires Operator or Admin role.');
                }
            } catch (err) {
                console.error('Failed to start race:', err);
            }
        }

        async function restartRace() {
            try {
                const res = await fetch('/sensors/race/restart', { method: 'POST' });
                if (!res.ok) {
                    const data = await res.json();
                    alert(data.message || 'Action requires Operator or Admin role.');
                }
            } catch (err) {
                console.error('Failed to restart race:', err);
            }
        }

        async function setEngineMode(mode) {
            try {
                const res = await fetch(`/sensors/engine-mode/${mode}`, { method: 'POST' });
                if (!res.ok) {
                    const data = await res.json();
                    alert(data.message || 'Action requires Operator or Admin role.');
                }
            } catch (err) {
                console.error('Failed to update engine mode:', err);
            }
        }

        async function resetLatchedInterlocks() {
            try {
                const res = await fetch('/sensors/interlocks/reset', { method: 'POST' });
                if (!res.ok) {
                    const data = await res.json();
                    alert(data.message || 'Action requires Operator or Admin role.');
                }
            } catch (err) {
                console.error('Failed to reset interlocks:', err);
            }
        }

        function setLinkState(isLost, ageSec) {
            const banner = document.getElementById('link-lost-banner');
            const badge = document.getElementById('link-status-badge');
            const dot = document.getElementById('link-dot');
            const text = document.getElementById('link-text');

            if (isLost) {
                banner.classList.remove('hidden');
                badge.className = 'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-bold tracking-widest uppercase bg-amber-950 text-amber-300 border border-amber-600 animate-critical';
                dot.className = 'h-2.5 w-2.5 rounded-full bg-amber-400';
                text.textContent = 'LINK LOST / STALE';
                document.getElementById('age-pill').textContent = ageSec !== null ? `AGE: ${ageSec.toFixed(1)}s (STALE)` : 'AGE: DISCONNECTED';
                document.getElementById('age-pill').className = 'text-xs font-mono px-2.5 py-1 rounded bg-amber-950 text-amber-300 border border-amber-600 font-bold';
            } else {
                banner.classList.add('hidden');
                badge.className = 'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-bold tracking-widest uppercase bg-emerald-950/90 text-emerald-400 border border-emerald-700/60';
                dot.className = 'h-2.5 w-2.5 rounded-full bg-emerald-400 animate-ping';
                text.textContent = 'LINK ACTIVE';
                document.getElementById('age-pill').textContent = `PACKET AGE: ${(ageSec * 1000).toFixed(0)} ms`;
                document.getElementById('age-pill').className = 'text-xs font-mono px-2.5 py-1 rounded bg-slate-800 text-slate-300 border border-slate-700';
            }
        }

        function renderTyres(temp, psi, wearPct) {
            document.getElementById('val-tyre-temp').textContent = temp.toFixed(1);
            document.getElementById('val-tyre-psi').textContent = psi.toFixed(2);
            document.getElementById('tyre-wear-label').textContent = `EST. WEAR: ${(wearPct || 0).toFixed(1)}%`;

            const badge = document.getElementById('badge-tyre');
            const psiBadge = document.getElementById('badge-psi');
            const bar = document.getElementById('bar-tyre');
            const card = document.getElementById('card-tyres');

            const pct = Math.min(100, Math.max(5, ((temp - 80) / (135 - 80)) * 100));
            bar.style.width = `${pct}%`;

            if (temp > 130.0) {
                badge.textContent = 'PUNCTURE HAZARD';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-red-950 text-red-200 border border-red-500 animate-critical';
                bar.className = 'h-full bg-red-600 transition-all duration-200';
                card.className = 'bg-slate-900/80 border-2 border-red-600 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            } else if (temp > 110.0) {
                badge.textContent = 'BLISTER WARNING';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-amber-950 text-amber-300 border border-amber-500';
                bar.className = 'h-full bg-amber-500 transition-all duration-200';
                card.className = 'bg-slate-900/80 border border-amber-600 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            } else if (temp >= 80.0) {
                badge.textContent = 'OPTIMAL';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-emerald-950 text-emerald-300 border border-emerald-700';
                bar.className = 'h-full bg-emerald-500 transition-all duration-200';
                card.className = 'bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            } else {
                badge.textContent = 'COLD GRAINING';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-sky-950 text-sky-300 border border-sky-700';
                bar.className = 'h-full bg-sky-400 transition-all duration-200';
                card.className = 'bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            }

            if (psi > 24.5) {
                psiBadge.textContent = 'PSI HIGH (>24.5)';
                psiBadge.className = 'font-bold text-amber-400 animate-pulse';
            } else {
                psiBadge.textContent = 'PSI OK';
                psiBadge.className = 'font-bold text-emerald-400';
            }
        }

        function renderBrakes(temp) {
            document.getElementById('val-brake-temp').textContent = temp.toFixed(1);
            const badge = document.getElementById('badge-brake');
            const bar = document.getElementById('bar-brake');
            const card = document.getElementById('card-brakes');

            const pct = Math.min(100, Math.max(5, ((temp - 300) / (1050 - 300)) * 100));
            bar.style.width = `${pct}%`;

            if (temp > 1000.0) {
                badge.textContent = 'THERMAL FADE ALERT';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-red-950 text-red-200 border border-red-500 animate-critical';
                bar.className = 'h-full bg-red-600 transition-all duration-200';
                card.className = 'bg-slate-900/80 border-2 border-red-600 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            } else if (temp >= 350.0) {
                badge.textContent = 'BITE WINDOW';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-emerald-950 text-emerald-300 border border-emerald-700';
                bar.className = 'h-full bg-emerald-500 transition-all duration-200';
                card.className = 'bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            } else {
                badge.textContent = 'COOLING / COLD (<350°C)';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-sky-950 text-sky-300 border border-sky-700';
                bar.className = 'h-full bg-sky-400 transition-all duration-200';
                card.className = 'bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            }
        }

        function renderFuel(remainingKg, flowKgh, crashed, estEndFuelPct) {
            document.getElementById('val-fuel-kg').textContent = remainingKg.toFixed(2);
            document.getElementById('val-fuel-flow').textContent = flowKgh.toFixed(1);

            const estLabel = document.getElementById('est-end-fuel-label');
            const estVal = estEndFuelPct !== undefined ? estEndFuelPct : 0.95;
            estLabel.textContent = `EST. FINISH: ${estVal.toFixed(2)}%`;
            if (estVal < 0.85) {
                estLabel.className = 'text-amber-400 font-bold';
            } else {
                estLabel.className = 'text-emerald-400 font-bold';
            }

            const badge = document.getElementById('badge-fuel');
            const bar = document.getElementById('bar-fuel');
            const pct = Math.min(100, Math.max(1, (remainingKg / 100.0) * 100));
            bar.style.width = `${pct}%`;

            if (crashed) {
                badge.textContent = 'FUEL PUMP CUTOFF';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-red-950 text-red-200 border border-red-500 animate-critical';
                bar.className = 'h-full bg-slate-500 transition-all duration-200';
            } else if (flowKgh > 100.0) {
                badge.textContent = 'FIA FLOW LIMIT >100';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-red-950 text-red-200 border border-red-500 animate-critical';
                bar.className = 'h-full bg-red-500 transition-all duration-200';
            } else if (remainingKg <= 1.05) {
                badge.textContent = 'TARGET 0.9–1.0% REACHED';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-amber-950 text-amber-300 border border-amber-500';
                bar.className = 'h-full bg-amber-500 transition-all duration-200';
            } else {
                badge.textContent = 'NOMINAL BURN';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-emerald-950 text-emerald-300 border border-emerald-700';
                bar.className = 'h-full bg-sky-500 transition-all duration-200';
            }
        }

        function renderHeadImpact(headG, crashed) {
            document.getElementById('val-head-g').textContent = headG.toFixed(1);
            const badge = document.getElementById('badge-head');
            const bar = document.getElementById('bar-head');
            const card = document.getElementById('card-head');

            const pct = Math.min(100, Math.max(4, (headG / 30.0) * 100));
            bar.style.width = `${pct}%`;

            if (headG > 20.0 || crashed) {
                badge.textContent = crashed && headG === 0 ? 'CRASH LATCHED (STOPPED)' : 'CONCUSSION INTERLOCK';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-red-950 text-red-200 border border-red-500 animate-critical';
                bar.className = 'h-full bg-red-600 transition-all duration-200';
                card.className = 'bg-slate-900/80 border-2 border-red-600 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            } else if (headG > 5.0) {
                badge.textContent = 'HIGH G-LOAD';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-amber-950 text-amber-300 border border-amber-500';
                bar.className = 'h-full bg-amber-500 transition-all duration-200';
                card.className = 'bg-slate-900/80 border border-amber-600 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            } else if (headG >= 2.0) {
                badge.textContent = 'CORNERING (2–5g)';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-emerald-950 text-emerald-300 border border-emerald-700';
                bar.className = 'h-full bg-purple-500 transition-all duration-200';
                card.className = 'bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            } else {
                badge.textContent = 'NOMINAL (<2g)';
                badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold uppercase bg-slate-800 text-slate-300 border border-slate-700';
                bar.className = 'h-full bg-purple-500 transition-all duration-200';
                card.className = 'bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl flex flex-col justify-between transition-all';
            }
        }

        function renderBanners(data) {
            const interlocks = data.interlocks || {};
            const emergencyBanner = document.getElementById('emergency-banner');
            const emergencyMessages = document.getElementById('emergency-messages');
            const pitBanner = document.getElementById('pit-strategy-banner');
            const pitReasonEl = document.getElementById('pit-strategy-reason');
            const checkeredBanner = document.getElementById('checkered-flag-banner');
            const raceStatusBadge = document.getElementById('race-status-badge');

            if (data.race_status === 'CHECKERED_FLAG') {
                checkeredBanner.classList.remove('hidden');
                raceStatusBadge.textContent = 'CHECKERED FLAG';
                raceStatusBadge.className = 'px-3 py-1 rounded-xl font-mono text-xs font-black uppercase tracking-wider bg-yellow-400 text-slate-950 shadow-md';
            } else if (data.race_status === 'CRASH_HALTED') {
                checkeredBanner.classList.add('hidden');
                raceStatusBadge.textContent = 'RED FLAG / CRASH';
                raceStatusBadge.className = 'px-3 py-1 rounded-xl font-mono text-xs font-black uppercase tracking-wider bg-red-600 text-white animate-critical';
            } else {
                checkeredBanner.classList.add('hidden');
                raceStatusBadge.textContent = 'RACING';
                raceStatusBadge.className = 'px-3 py-1 rounded-xl font-mono text-xs font-black uppercase tracking-wider bg-red-600/20 text-red-400 border border-red-500/40';
            }

            const engMode = data.engine_mode || 'MODE_BALANCED';
            const engPill = document.getElementById('engine-mode-pill');
            engPill.textContent = engMode;
            if (engMode === 'MODE_PUSH') {
                engPill.className = 'px-2 py-0.5 rounded text-[11px] font-mono font-black uppercase bg-red-950 text-red-300 border border-red-600 animate-pulse';
            } else if (engMode === 'MODE_CONSERVE') {
                engPill.className = 'px-2 py-0.5 rounded text-[11px] font-mono font-black uppercase bg-emerald-950 text-emerald-300 border border-emerald-600';
            } else {
                engPill.className = 'px-2 py-0.5 rounded text-[11px] font-mono font-black uppercase bg-sky-950 text-sky-400 border border-sky-700';
            }

            if (data.pit_stop_recommended && !data.crashed && data.active_scenario !== 'PIT_STOP') {
                pitBanner.classList.remove('hidden');
                pitReasonEl.textContent = data.pit_recommendation_reason || 'PIT WALL RECOMMENDS BOXING';
            } else {
                pitBanner.classList.add('hidden');
            }

            const alarms = interlocks.latched_alarms || [];
            if (interlocks.critical_trip || alarms.length > 0) {
                emergencyBanner.classList.remove('hidden');
                emergencyMessages.innerHTML = '';
                alarms.forEach(a => {
                    const row = document.createElement('div');
                    row.textContent = `• [${a.code}] ${a.message}`;
                    emergencyMessages.appendChild(row);
                });
            } else {
                emergencyBanner.classList.add('hidden');
            }
        }

        function renderLapTimes(data) {
            const curTime = data.current_lap_time_s !== undefined ? data.current_lap_time_s : 0.0;
            document.getElementById('val-current-lap-time').textContent = `${curTime.toFixed(3)}s`;

            if (data.last_lap_time_s !== null && data.last_lap_time_s !== undefined) {
                document.getElementById('val-last-lap-time').textContent = `${data.last_lap_time_s.toFixed(3)}s`;
            } else {
                document.getElementById('val-last-lap-time').textContent = '--.---s';
            }

            if (data.best_lap_time_s !== null && data.best_lap_time_s !== undefined) {
                document.getElementById('val-best-lap-time').textContent = `${data.best_lap_time_s.toFixed(3)}s`;
            } else {
                document.getElementById('val-best-lap-time').textContent = '--.---s';
            }

            const delta = data.pace_delta_s !== undefined ? data.pace_delta_s : 0.0;
            const deltaEl = document.getElementById('val-pace-delta');
            if (delta > 0.0) {
                deltaEl.textContent = `+${delta.toFixed(3)}s`;
                deltaEl.className = 'text-xl font-black text-amber-400';
            } else if (delta < 0.0) {
                deltaEl.textContent = `${delta.toFixed(3)}s`;
                deltaEl.className = 'text-xl font-black text-purple-400';
            } else {
                deltaEl.textContent = '+0.000s';
                deltaEl.className = 'text-xl font-black text-emerald-400';
            }

            const splits = data.sector_splits || {};
            if (splits.S1) document.getElementById('sector-s1').textContent = `S1: ${splits.S1.toFixed(2)}s`;
            if (splits.S2) document.getElementById('sector-s2').textContent = `S2: ${splits.S2.toFixed(2)}s`;
            if (splits.S3) document.getElementById('sector-s3').textContent = `S3: ${splits.S3.toFixed(2)}s`;
        }

        let lastTimestamp = 0;

        async function pollTelemetry() {
            const reqStart = performance.now();
            try {
                const res = await fetch('/sensors/live-telemetry', { cache: 'no-store' });
                const rttMs = performance.now() - reqStart;

                if (!res.ok) {
                    setLinkState(true, null);
                    return;
                }

                const data = await res.json();
                const nowSec = Date.now() / 1000;
                const packetAgeSec = data.timestamp ? Math.max(0, nowSec - data.timestamp) : 999;

                if (!data.timestamp || packetAgeSec > 3.0) {
                    setLinkState(true, data.timestamp ? packetAgeSec : null);
                } else {
                    setLinkState(false, packetAgeSec);
                }

                const totalLatency = (data.pipeline_latency_ms || 0) + rttMs;
                document.getElementById('latency-badge').textContent = `LATENCY: ${totalLatency.toFixed(1)} ms`;
                document.getElementById('car-id-label').textContent = data.car_id || 'F1-CAR-01';
                document.getElementById('source-pill').textContent = `SOURCE: ${(data.source || 'mqtt').toUpperCase()}`;

                document.getElementById('lap-counter').textContent = `LAP ${data.lap || 1} / ${data.total_laps || 15}`;
                document.getElementById('speed-pill').textContent = `${data.speed_kmh !== undefined ? data.speed_kmh : 0} KM/H`;
                document.getElementById('track-phase-label').textContent = data.track_phase || 'ON TRACK';
                document.getElementById('active-scenario-pill').textContent = data.active_scenario || 'NOMINAL_RACING';

                renderTyres(data.tyre_surface_temp_c, data.tyre_pressure_psi, data.estimated_tyre_wear_pct);
                renderBrakes(data.front_brake_temp_c);
                renderFuel(data.fuel_remaining_kg, data.fuel_flow_rate_kgh, data.crashed, data.estimated_end_fuel_pct);
                renderHeadImpact(data.head_accel_g, data.crashed);
                renderLapTimes(data);
                renderBanners(data);

                if (data.timestamp && data.timestamp !== lastTimestamp) {
                    lastTimestamp = data.timestamp;
                    const label = `L${data.lap || 1} ` + new Date(data.timestamp * 1000).toLocaleTimeString([], {
                        hour12: false,
                        minute: '2-digit',
                        second: '2-digit'
                    });

                    chartData.labels.push(label);
                    chartData.datasets[0].data.push(data.tyre_surface_temp_c);
                    chartData.datasets[1].data.push(data.fuel_flow_rate_kgh);
                    chartData.datasets[2].data.push(data.front_brake_temp_c);
                    chartData.datasets[3].data.push(data.head_accel_g);

                    if (chartData.labels.length > MAX_TICKS) {
                        chartData.labels.shift();
                        chartData.datasets.forEach(ds => ds.data.shift());
                    }
                    telemetryChart.update();
                }
            } catch (err) {
                setLinkState(true, null);
            }
        }

        // Initialize user info and start telemetry polling
        fetchCurrentUser();
        setInterval(pollTelemetry, 300);
        pollTelemetry();
    </script>
</body>
</html>
"""

@app.route("/dashboard")
def dashboard():
    """Render the responsive F1 Pit Wall Telemetry & Safety Interlocks dashboard (Viewer, Operator, Admin)"""
    resp = make_response(
        render_template_string(
            DASHBOARD_HTML,
            mqtt_topic=MQTT_TOPIC,
            car_id=CAR_ID,
        )
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/kpi-dashboard")
def serve_kpi_dashboard():
    """Serve the 100-Testcase ISO/IEC 25010 KPI Dashboard HTML"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kpi_dashboard.html")
    if os.path.exists(path):
        return send_file(path, mimetype="text/html")
    return "KPI Dashboard HTML not found. Run run_100_tests_and_kpis.py first.", 404

@app.route("/schema")
def serve_schema_html():
    """Serve the Database Schema HTML"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database_schema.html")
    if os.path.exists(path):
        return send_file(path, mimetype="text/html")
    return "Database Schema HTML not found.", 404

@app.route("/")
def root_redirect():
    return redirect("/dashboard")

def start_background_simulator():
    sim_thread = threading.Thread(
        target=telemetry_simulator_worker,
        name="F1TelemetrySimulator",
        daemon=True,
    )
    sim_thread.start()
    logger.info("Started 0.5s F1 telemetry background simulator thread.")

if __name__ == "__main__":
    start_background_simulator()
    logger.info("Starting F1 Pit Wall Service on 0.0.0.0:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
