"""
End-to-End Automated Role-Based Access Control (RBAC) Tests
Module: Digital Integration and Systems Testing (Cranfield University)
Executed via pytest-playwright with 1000ms slow_mo and automated video recording.
"""
import os
import time
import pytest
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("PITWALL_TEST_URL", "http://127.0.0.1:5000")
VIDEOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")
os.makedirs(VIDEOS_DIR, exist_ok=True)

@pytest.fixture(scope="module")
def browser_instance():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, slow_mo=1000)
        yield browser
        browser.close()

def create_recording_context(browser, test_name: str):
    """Creates a browser context with video recording in the videos directory"""
    return browser.new_context(
        record_video_dir=VIDEOS_DIR,
        record_video_size={"width": 1280, "height": 720},
        viewport={"width": 1280, "height": 720},
    )

def test_1_unsuccessful_authentication(browser_instance):
    """
    Test 1: Unsuccessful authentication:
    Verify invalid credentials show an error and block access.
    """
    context = create_recording_context(browser_instance, "test_1_unsuccessful_auth")
    page = context.new_page()

    page.goto(f"{BASE_URL}/dashboard")
    page.wait_for_selector("#btn-login", state="visible")

    # Open Login Modal
    page.click("#btn-login")
    page.wait_for_selector("#login-modal", state="visible")

    # Attempt login with invalid credentials
    page.fill("#login-username", "invalid_user")
    page.fill("#login-password", "wrong_password_999")
    page.click("#login-form button[type='submit']")

    # Verify error message is displayed
    page.wait_for_selector("#login-error-msg", state="visible")
    err_text = page.text_content("#login-error-msg")
    assert "Invalid credentials" in err_text or "Error" in err_text

    # Verify still in Viewer/Guest state
    role_text = page.text_content("#current-user-role")
    assert "VIEWER" in role_text.upper()

    page.wait_for_timeout(1000)
    page.close()
    context.close()

def test_2_viewer_role(browser_instance):
    """
    Test 2: Viewer role:
    Verify login succeeds, dashboard is visible, but sensor editing and user management are inaccessible.
    """
    context = create_recording_context(browser_instance, "test_2_viewer_role")
    page = context.new_page()

    page.goto(f"{BASE_URL}/dashboard")
    page.click("#btn-login")
    page.wait_for_selector("#login-modal", state="visible")

    # Log in as Viewer
    page.fill("#login-username", "viewer1")
    page.fill("#login-password", "viewer123")
    page.click("#login-form button[type='submit']")

    # Verify login succeeded and role updated
    page.wait_for_selector("#btn-logout", state="visible")
    assert page.text_content("#current-user-name") == "viewer1"
    assert "VIEWER" in page.text_content("#current-user-role").upper()

    # Verify dashboard telemetry is visible
    assert page.is_visible("#telemetryChart")
    assert page.is_visible("#card-tyres")

    # Verify sensor editing form is NOT visible to Viewer
    assert not page.is_visible("#create-sensor-form")
    # Verify Admin User Management panel is NOT visible to Viewer
    assert not page.is_visible("#admin-user-panel")

    # Verify direct REST API request to /users is blocked with 401/403
    status = page.evaluate("async () => { const res = await fetch('/users'); return res.status; }")
    assert status in (401, 403), f"Expected 401 or 403 for Viewer accessing /users, got {status}"

    page.wait_for_timeout(1000)
    page.close()
    context.close()

def test_3_operator_role(browser_instance):
    """
    Test 3: Operator role:
    Verify login succeeds, dashboard and sensor CRUD are accessible, but user management is forbidden.
    """
    context = create_recording_context(browser_instance, "test_3_operator_role")
    page = context.new_page()

    page.goto(f"{BASE_URL}/dashboard")
    page.click("#btn-login")
    page.wait_for_selector("#login-modal", state="visible")

    # Log in as Operator
    page.fill("#login-username", "operator1")
    page.fill("#login-password", "operator123")
    page.click("#login-form button[type='submit']")

    # Verify login succeeded and role updated
    page.wait_for_selector("#btn-logout", state="visible")
    assert page.text_content("#current-user-name") == "operator1"
    assert "OPERATOR" in page.text_content("#current-user-role").upper()

    # Verify dashboard and sensor CRUD panel are accessible
    assert page.is_visible("#card-tyres")
    assert page.is_visible("#create-sensor-form")
    assert page.is_visible("#btn-add-sensor")

    # Register a new sensor channel via Operator permissions
    test_sensor_name = f"opt_temp_{int(time.time())}"
    page.fill("#sensor-name-input", test_sensor_name)
    page.fill("#sensor-loc-input", "Rear Wing Pylon")
    page.fill("#sensor-type-input", "Aero Load (N)")
    page.fill("#sensor-freq-input", "4 Hz (0.25s)")
    page.click("#btn-add-sensor")

    page.wait_for_timeout(1500)
    sensors_table = page.text_content("#sensors-table-body")
    assert test_sensor_name in sensors_table, f"Expected {test_sensor_name} in sensors table"

    # Verify user management is forbidden (hidden on UI and 403 via REST)
    assert not page.is_visible("#admin-user-panel")
    status = page.evaluate("async () => { const res = await fetch('/users'); return res.status; }")
    assert status in (401, 403), f"Expected 401/403 for Operator accessing /users, got {status}"

    page.wait_for_timeout(1000)
    page.close()
    context.close()

def test_4_admin_role(browser_instance):
    """
    Test 4: Admin role:
    Verify login succeeds with full access to dashboard, sensor CRUD, and user management.
    """
    context = create_recording_context(browser_instance, "test_4_admin_role")
    page = context.new_page()

    page.goto(f"{BASE_URL}/dashboard")
    page.click("#btn-login")
    page.wait_for_selector("#login-modal", state="visible")

    # Log in as Admin
    page.fill("#login-username", "admin")
    page.fill("#login-password", "admin123")
    page.click("#login-form button[type='submit']")

    # Verify login succeeded and role is Admin
    page.wait_for_selector("#btn-logout", state="visible")
    assert page.text_content("#current-user-name") == "admin"
    assert "ADMIN" in page.text_content("#current-user-role").upper()

    # Full access checks:
    # 1. Dashboard visible
    assert page.is_visible("#telemetryChart")
    assert page.is_visible("#card-brakes")

    # 2. Sensor CRUD visible and operable
    assert page.is_visible("#create-sensor-form")

    # 3. User Management visible and operable
    page.wait_for_selector("#admin-user-panel", state="visible")
    assert page.is_visible("#create-user-form")

    # Admin creates a new user via the UI form
    new_test_user = f"pilot_{int(time.time()) % 10000}"
    page.fill("#new-user-name", new_test_user)
    page.fill("#new-user-pass", "secretPass99")
    page.select_option("#new-user-role", "Viewer")
    page.click("#create-user-form button[type='submit']")

    page.wait_for_timeout(1500)
    users_table = page.text_content("#users-table-body")
    assert new_test_user in users_table, f"Expected {new_test_user} in users table"

    # Verify Admin has 200 OK access to /users REST endpoint
    status = page.evaluate("async () => { const res = await fetch('/users'); return res.status; }")
    assert status == 200, f"Expected 200 OK for Admin accessing /users, got {status}"

    page.wait_for_timeout(1000)
    page.close()
    context.close()
