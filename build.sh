#!/usr/bin/env bash
# Build script for Render deployment

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Install gunicorn if not already in requirements.txt
pip install gunicorn

# Install Node.js dependencies if package.json exists
if [ -f "package.json" ]; then
    npm install
fi
