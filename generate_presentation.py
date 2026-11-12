import os
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE

def create_deck(filename):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    # Color Palette: F1 Dark Theme
    COLOR_BG = RGBColor(11, 15, 25)         # slate-950
    COLOR_CARD = RGBColor(22, 29, 47)       # slate-900
    COLOR_RED = RGBColor(220, 38, 38)       # f1 red
    COLOR_EMERALD = RGBColor(16, 185, 129)  # accent green
    COLOR_SKY = RGBColor(14, 165, 233)      # sky blue
    COLOR_AMBER = RGBColor(245, 158, 11)    # warning amber
    COLOR_WHITE = RGBColor(248, 250, 252)   # text white
    COLOR_MUTED = RGBColor(148, 163, 184)   # text slate-400

    def add_slide_base(title_text, category_tag, timing_text=""):
        slide = prs.slides.add_slide(blank_layout)
        
        # Background
        bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
        bg.fill.solid()
        bg.fill.fore_color.rgb = COLOR_BG
        bg.line.fill.background()

        # Accent red bar
        bar = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.8), Inches(0.5), Inches(0.15), Inches(0.85))
        bar.fill.solid()
        bar.fill.fore_color.rgb = COLOR_RED
        bar.line.fill.background()

        # Header Box
        header_box = slide.shapes.add_textbox(Inches(1.1), Inches(0.4), Inches(11.4), Inches(1.0))
        tf = header_box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_top = tf.margin_right = tf.margin_bottom = 0
        
        p_tag = tf.paragraphs[0]
        p_tag.text = f"{category_tag.upper()}   |   CRANFIELD UNIVERSITY — DI & ST"
        p_tag.font.name = "Arial"
        p_tag.font.size = Pt(10)
        p_tag.font.bold = True
        p_tag.font.color.rgb = COLOR_SKY

        p_title = tf.add_paragraph()
        p_title.text = title_text
        p_title.font.name = "Arial"
        p_title.font.size = Pt(22)
        p_title.font.bold = True
        p_title.font.color.rgb = COLOR_WHITE

        # Timing tag in top right
        if timing_text:
            time_box = slide.shapes.add_textbox(Inches(10.5), Inches(0.45), Inches(2.0), Inches(0.5))
            ttf = time_box.text_frame
            tp = ttf.paragraphs[0]
            tp.alignment = PP_ALIGN.RIGHT
            tp.text = timing_text
            tp.font.name = "Arial"
            tp.font.size = Pt(11)
            tp.font.bold = True
            tp.font.color.rgb = COLOR_AMBER

        # Footer
        footer_box = slide.shapes.add_textbox(Inches(0.8), Inches(7.0), Inches(11.7), Inches(0.35))
        ftf = footer_box.text_frame
        fp = ftf.paragraphs[0]
        fp.text = "Digital Integration & Systems Testing  •  F1 Pit Wall Telemetry & Safety Interlocks  •  github.com/dan-wish/DI_And_ST"
        fp.font.name = "Arial"
        fp.font.size = Pt(9)
        fp.font.color.rgb = COLOR_MUTED

        return slide

    def add_card(slide, left, top, width, height, title, bullet_items, border_color=None):
        card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
        card.fill.solid()
        card.fill.fore_color.rgb = COLOR_CARD
        if border_color:
            card.line.color.rgb = border_color
            card.line.width = Pt(1.5)
        else:
            card.line.color.rgb = RGBColor(30, 41, 59)
            card.line.width = Pt(1)

        tx_box = slide.shapes.add_textbox(left + Inches(0.2), top + Inches(0.2), width - Inches(0.4), height - Inches(0.4))
        tf = tx_box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_top = tf.margin_right = tf.margin_bottom = 0

        p_t = tf.paragraphs[0]
        p_t.text = title
        p_t.font.name = "Arial"
        p_t.font.size = Pt(14)
        p_t.font.bold = True
        p_t.font.color.rgb = COLOR_EMERALD if not border_color else border_color

        for b in bullet_items:
            p = tf.add_paragraph()
            p.text = f"•  {b}"
            p.font.name = "Arial"
            p.font.size = Pt(11)
            p.font.color.rgb = COLOR_WHITE
            p.space_before = Pt(4)

    # =========================================================================
    # SLIDE 1: TITLE SLIDE
    # =========================================================================
    s1 = prs.slides.add_slide(blank_layout)
    bg1 = s1.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
    bg1.fill.solid()
    bg1.fill.fore_color.rgb = COLOR_BG
    bg1.line.fill.background()

    # Title box
    tbox = s1.shapes.add_textbox(Inches(1.0), Inches(1.8), Inches(11.33), Inches(4.0))
    tf1 = tbox.text_frame
    tf1.word_wrap = True
    
    p = tf1.paragraphs[0]
    p.text = "CRANFIELD UNIVERSITY — SCHOOL OF AEROSPACE, TRANSPORT AND MANUFACTURING"
    p.font.size = Pt(12)
    p.font.bold = True
    p.font.color.rgb = COLOR_SKY

    p = tf1.add_paragraph()
    p.text = "F1 Pit Wall Telemetry & Safety Interlocks"
    p.font.size = Pt(36)
    p.font.bold = True
    p.font.color.rgb = COLOR_WHITE
    p.space_before = Pt(12)

    p = tf1.add_paragraph()
    p.text = "A Containerised IoT Demonstrator for Digital Integration & Systems Testing (DI & ST)"
    p.font.size = Pt(18)
    p.font.color.rgb = COLOR_RED
    p.space_before = Pt(6)

    p = tf1.add_paragraph()
    p.text = "Problem Statement: \"Maximise track performance within strict thermal, safety, and regulatory constraints.\""
    p.font.size = Pt(13)
    p.font.italic = True
    p.font.color.rgb = COLOR_MUTED
    p.space_before = Pt(16)

    p = tf1.add_paragraph()
    p.text = "Presenter: Daniel Wishart  |  Module: Digital Integration and Systems Testing  |  Time: 10 Minutes"
    p.font.size = Pt(11)
    p.font.bold = True
    p.font.color.rgb = COLOR_EMERALD
    p.space_before = Pt(24)

    # =========================================================================
    # SLIDE 2: STAGE 1 - THE PROBLEM (YOUR X & Y)
    # =========================================================================
    s2 = add_slide_base("Stage 1: Problem Definition, Operational Context & Scope", "Stage 1: Problem", "1.5 Mins")
    add_card(s2, Inches(0.8), Inches(1.6), Inches(3.6), Inches(5.1), "Core Operational Challenge", [
        "F1 cars operate on the absolute physical threshold of tyres, brakes, and fuel.",
        "Engineers must extract maximum lap pace while ensuring thermal interlocks are never violated.",
        "Demonstrator Target User: Pit Wall Race Engineers, Chief Strategists, and FIA Technical Stewards.",
        "Demonstrator Purpose: Provide automated deterministic interlocks and live telemetry analytics."
    ])
    add_card(s2, Inches(4.8), Inches(1.6), Inches(3.6), Inches(5.1), "The X & Y Scope Definition", [
        "X (Edge Telemetry Ingestion): Continuous high-frequency IoT streaming over MQTT topic 'telemetry/car/1'.",
        "Edge physics simulation model tracks 15-lap Grand Prix, realistic tyre degradation, and fuel burn.",
        "Y (Pit Wall Interlocks & Display): Deterministic Python interlocks evaluating real-time safety thresholds.",
        "Immediate fail-safe state & latched alarms on thermal fade, tyre punctures, or high-G impacts."
    ])
    add_card(s2, Inches(8.8), Inches(1.6), Inches(3.7), Inches(5.1), "4 Scoped Sensor Subsystems", [
        "1. Tyres: Surface Temp (80–110°C optimal, >130°C puncture trip) + TPMS Pressure (>24.5 psi alert).",
        "2. Brakes: Carbon Disc Temp (>350°C bite window, >1000°C thermal fade hazard).",
        "3. Fuel: Remaining Tank Mass (burn to 0.95% target) + FIA Fuel Flow Limit (100 kg/h max).",
        "4. Driver Earpiece: In-ear tri-axial MEMS accelerometer (>20g medical concussion trip)."
    ])

    # =========================================================================
    # SLIDE 3: STAGE 2 - INTEGRATE (LO1 & TWELVE-FACTOR)
    # =========================================================================
    s3 = add_slide_base("Stage 2: Architecture, Integration Strategy & Twelve-Factor Design", "Stage 2: Integrate", "1.5 Mins")
    add_card(s3, Inches(0.8), Inches(1.6), Inches(3.6), Inches(5.1), "Integration Order & Justification", [
        "Followed classical bottom-up V-Model integration sequence:",
        "Layer 1: Backing Service — Eclipse Mosquitto MQTT Broker (port 1883) for isolated message bus.",
        "Layer 2: Vehicle Test Driver / Stub — Embedded thread simulating realistic Gaussian physics drift.",
        "Layer 3: Ingestion API — Flask + Flask-RESTX REST layer on port 5000 with OpenAPI Swagger UI.",
        "Layer 4: Presentation — Tailwind CSS & Chart.js live pit wall dashboard with sub-300ms polling."
    ])
    add_card(s3, Inches(4.8), Inches(1.6), Inches(3.6), Inches(5.1), "Twelve-Factor Scorecard", [
        "I. One codebase in Git (github.com/dan-wish/DI_And_ST).",
        "II. Explicit dependencies via requirements.txt.",
        "III. Config in Environment (MQTT_BROKER, PORT, DB_PATH).",
        "IV. Backing services attached via URL/host configs.",
        "VI. Stateless app processes; SQLite for state persistence.",
        "VII. Self-contained port binding (0.0.0.0:5000).",
        "XI. Logs treated as event stream (unbuffered stdout)."
    ])
    add_card(s3, Inches(8.8), Inches(1.6), Inches(3.7), Inches(5.1), "Edge Ingestion & Driver Stubs", [
        "Telemetry simulator worker acts as vehicle test driver generating realistic track sectors (S1/S2/S3).",
        "Gaussian normal distribution: random.gauss(25.0, 10.0) simulates dynamic atmospheric and tyre variances.",
        "Seamless decoupling: broker allows real Arduino/CAN edge hardware or mock driver without API changes."
    ])

    # =========================================================================
    # SLIDE 4: STAGE 3 - CONTROL (LO2, ISO 25010 & CM)
    # =========================================================================
    s4 = add_slide_base("Stage 3: Quality Control, ISO/IEC 25010 Attributes & Git CM", "Stage 3: Control", "1.5 Mins")
    add_card(s4, Inches(0.8), Inches(1.6), Inches(5.6), Inches(5.1), "ISO/IEC 25010 Quality Attributes Appraisal", [
        "Safety (Functional Safety): Deterministic interlock engine latches alarms; does not rely on async queues.",
        "Performance Efficiency: Ingestion-to-display latency <150ms SLA; achieved ~0.6ms mean server response.",
        "Resilience (Fault Tolerance): Client detects broken MQTT stream (>3.0s) and triggers LINK LOST fail-safe banner.",
        "Security: Salted PBKDF2 password hashing; Role-Based Access Control (Viewer, Operator, Admin).",
        "Compatibility / I18n: Endpoints accept international UTF-8 Cyrillic, Chinese, and accented car IDs."
    ])
    add_card(s4, Inches(6.8), Inches(1.6), Inches(5.7), Inches(5.1), "Configuration Management (ISO 9001 / 29119)", [
        "Strict Git branching model used throughout development:",
        "• feature: RBAC User models, login modal, and admin user management.",
        "• dev: Gaussian temperature distribution telemetry generator.",
        "• main: Clean, tested release branch with --no-ff merge history.",
        "Semantic Versioning & Release Tagging: Annotated release tag V1.0 created and verified.",
        "Public Source Control: All code, migrations, tests, and video artifacts uploaded to github.com/dan-wish/DI_And_ST."
    ])

    # =========================================================================
    # SLIDE 5: STAGE 4 - TEST (LO3, AUTOMATED 100-TEST SUITE & E2E)
    # =========================================================================
    s5 = add_slide_base("Stage 4: Test Strategy, Systematic Verification & Evidence", "Stage 4: Test", "2.0 Mins")
    add_card(s5, Inches(0.8), Inches(1.6), Inches(3.6), Inches(5.1), "Automated 100-Test Suite", [
        "Executed via run_100_tests_and_kpis.py across 3 rigorous testing categories:",
        "• 35 Functional Tests: Sensor registry CRUD, multi-language car IDs, fuel curve verification.",
        "• 35 Security Tests: Boundary values, out-of-range sensor readings, SQL injection payloads.",
        "• 30 Performance Tests: High-frequency burst ingestion and UI polling latency SLA benchmarks.",
        "Reported via interactive kpi_dashboard.html."
    ])
    add_card(s5, Inches(4.8), Inches(1.6), Inches(3.6), Inches(5.1), "Boundary & Equivalence Testing", [
        "Tyre Temp Equivalence Partitions: Cold (<80°C), Optimal (80–110°C), Blister (110–130°C), Puncture (>130°C).",
        "Brake Boundaries: <350°C (Cold), 350–1000°C (Bite Window), >1000°C (Thermal Fade Trip).",
        "Security Red-Teaming: Injection payloads with Russian ('Датчик_Шин'), Chinese ('赛车_88'), and SQLi (' OR '1'='1) safely rejected with HTTP 400."
    ])
    add_card(s5, Inches(8.8), Inches(1.6), Inches(3.7), Inches(5.1), "Playwright Automated E2E Suite", [
        "pytest-playwright test suite in test_auth_e2e.py executed in headless Chromium with slow_mo=1000ms:",
        "1. Unsuccessful Authentication (invalid credentials rejected).",
        "2. Viewer Role (read-only dashboard, controls hidden).",
        "3. Operator Role (race start/restart, sensor CRUD allowed).",
        "4. Admin Role (full access + /users management).",
        "100% Pass Rate (4/4 passed). Video proof in videos/."
    ])

    # =========================================================================
    # SLIDE 6: STAGE 5 - ASSURE (LO4, HAZARD ANALYSIS & ALARP)
    # =========================================================================
    s6 = add_slide_base("Stage 5: Safety Assurance, Hazard Mitigation (ALARP) & The AI Lens", "Stage 5: Assure", "1.5 Mins")
    add_card(s6, Inches(0.8), Inches(1.6), Inches(5.6), Inches(5.1), "Hazard Register & ALARP Argument", [
        "Hazard 1 — Tyre Delamination (>130°C): ALARP action latches 'PUNCTURE HAZARD' alarm and prompts instant 'BOX BOX' pit call.",
        "Hazard 2 — Brake Fluid Boiling (>1000°C): Latches 'BRAKE THERMAL FADE' alarm; commands aerodynamic lift & coast.",
        "Hazard 3 — Severe Crash (>20g Driver Impact): Medical Concussion Interlock immediately kills telemetry fuel pump and trips red flag.",
        "Hazard 4 — FIA Fuel Flow Violation (>100 kg/h): Latches technical regulation flag for post-race scrutiny.",
        "Assurance Argument: Deterministic Python interlocks guarantee zero unlatched critical failures."
    ])
    add_card(s6, Inches(6.8), Inches(1.6), Inches(5.7), Inches(5.1), "The AI Thread (Course Reflection)", [
        "Monday: Scaffolding Twelve-Factor stubs with AI; storing secrets in environment variables.",
        "Tuesday: Reviewing AI-generated code against ISO 25010 attributes; ensuring deterministic interlocks.",
        "Wednesday: Checking AI-generated test cases; applying red-teaming (SQLi, Unicode, boundary values).",
        "Thursday: Safety Guardrails OUTSIDE the Model — critical safety trips are hard-coded deterministic boolean checks, never probabilistic AI heuristics."
    ])

    # =========================================================================
    # SLIDE 7: STAGE 6 - DEMO (LIVE DEMONSTRATOR & ERGONOMICS)
    # =========================================================================
    s7 = add_slide_base("Stage 6: Live Demonstrator Walkthrough & UI Ergonomics", "Stage 6: Demo", "2.0 Mins")
    add_card(s7, Inches(0.8), Inches(1.6), Inches(3.6), Inches(5.1), "Ergonomic Pit Wall Deck", [
        "Persistent Top Viewspace Capsule:",
        "• Always-visible Track Location: S1, S2, S3 corner/straight status.",
        "• Current Lap, Speed, and Status pinned to sticky top header.",
        "• Sticky Secondary Telemetry Strip: Instant glance of tyre temp, brake temp, fuel, and G-force even when scrolling."
    ])
    add_card(s7, Inches(4.8), Inches(1.6), Inches(3.6), Inches(5.1), "Interactive Scenario Injection", [
        "Live Demonstartion Steps:",
        "1. Nominal racing: Gaussian drift, tyre wear counter, fuel burn towards 0.95% target.",
        "2. Inject Scenario: 'Brake Debris' (>1000°C) or 'Simulate Crash' (>20g).",
        "3. Observe immediate latched red banner, audible alert, and pit call.",
        "4. Acknowledge and reset interlock via Operator controls."
    ])
    add_card(s7, Inches(8.8), Inches(1.6), Inches(3.7), Inches(5.1), "RBAC Personas & Sign Up", [
        "1. Sign Up Modal: Register on the fly as Viewer or Operator directly from UI.",
        "2. Viewer persona: Observes real-time telemetry; controls are disabled.",
        "3. Operator persona: Controls race start/restart, scenarios, engine modes, and sensor registry.",
        "4. Admin persona: Unlocks RBAC User Management (/users)."
    ])

    # =========================================================================
    # SLIDE 8: SUMMARY & CONCLUSION
    # =========================================================================
    s8 = add_slide_base("Summary of Learning Outcomes & Engineering Deliverables", "Conclusion", "0.5 Mins")
    add_card(s8, Inches(0.8), Inches(1.6), Inches(5.6), Inches(5.1), "Evidence Alignment Against LO1–LO4", [
        "LO1 (Integration): Successfully integrated 4-tier IoT pipeline using Twelve-Factor principles and MQTT stubs.",
        "LO2 (Testing Standards): Implemented ISO/IEC 25010 attributes with rigorous Git branching and release tagging (V1.0).",
        "LO3 (Test Management): Designed and executed 100-test automated suite + Playwright E2E with video proof.",
        "LO4 (Assurance): Built deterministic safety interlocks satisfying ALARP criteria with responsible AI guardrails."
    ])
    add_card(s8, Inches(6.8), Inches(1.6), Inches(5.7), Inches(5.1), "Repository & Deliverables Access", [
        "• Public GitHub Repository: https://github.com/dan-wish/DI_And_ST",
        "• Live Dashboard: http://localhost:5000/dashboard",
        "• Interactive OpenAPI Docs: http://localhost:5000/swagger/",
        "• 100-Test KPI Dashboard: http://localhost:5000/kpi-dashboard",
        "• Database Schema: http://localhost:5000/schema",
        "Thank you! Ready for questions and live system evaluation."
    ], border_color=COLOR_EMERALD)

    prs.save(filename)
    print(f"Presentation saved successfully to {filename}")

if __name__ == "__main__":
    out_path = r"C:\Users\danie\Desktop\F\DI_And_ST\F1_Telemetry_Presentation_10Min.pptx"
    create_deck(out_path)
