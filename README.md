# Echoes of Tomorrow - Central Unit

This repository contains the core server-side (Master) logic for the **Echoes of Tomorrow** light installation project, developed by Fontys ICT for GLOW Eindhoven 2025. This is done with a Mac Mini.

## Overview
The central unit is responsible for:
- Receiving and processing audio signals
- Sending commands to microcontrollers controlling the LED arms, LED matrix and Toppiece
- Keeping track of the state
- Hosting a control interface

## Project Structure
```
central-unit/
├── core/               # Python logic for core processing
├── microphones/        # Audio input processing modules
├── scripts/            # Setup and utility scripts
├── tests/              # Unit and integration tests
├── tools/              # Calibration and diagnostics tools
└── documentation/      # Technical documentation
```

## Setup Instructions
1. Clone the repository:
   ```bash
   git clone https://github.com/echoes-of-tomorrow/central-unit.git
   cd central-unit
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # or venv\Scripts\activate on Windows
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running the Central Unit
Run the main script:
```bash
---
```

## Testing
```bash
---
```

## Documentation
See `documentation/` or the [Wiki](https://github.com/GLOW-Delta-2025/central-unit/wiki) for details on architecture, function descriptions, and setup.

## Branches
- `main`: Production-ready code
- `develop`: Active development
- `feature/<name>`, `bugfix/<name>`, `hotfix/<name>`: Use Git Flow

## Commit Convention
```text
<type>: <short description>
```
Example: `fix: LED flickering on pin 6`
