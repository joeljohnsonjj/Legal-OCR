#!/usr/bin/env python
"""
Quick build script for session_manager library.
Builds wheel and source distribution ready for pip install.
"""
import subprocess
import sys
import os
from pathlib import Path

def check_requirements():
    """Check if build tools are installed."""
    try:
        import build
        import setuptools
        return True
    except ImportError:
        return False

def install_build_tools():
    """Install required build tools."""
    print("Installing build tools...")
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", 
            "--upgrade", "build", "setuptools", "wheel"
        ])
        return True
    except subprocess.CalledProcessError:
        return False

def clean_previous_builds():
    """Remove previous build artifacts."""
    print("Cleaning previous builds...")
    
    dirs_to_clean = ["build", "dist", "session_manager.egg-info"]
    for dir_name in dirs_to_clean:
        dir_path = Path(dir_name)
        if dir_path.exists():
            import shutil
            shutil.rmtree(dir_path)
            print(f"  Removed {dir_name}/")
    
    print("Clean complete")

def build_package():
    """Build the package using python-build."""
    print("\nBuilding package...")
    try:
        subprocess.check_call([sys.executable, "-m", "build"])
        return True
    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")
        return False

def verify_build():
    """Verify the built packages."""
    print("\nVerifying build...")
    
    dist_path = Path("dist")
    if not dist_path.exists():
        print("Error: dist/ directory not found")
        return False
    
    wheels = list(dist_path.glob("*.whl"))
    tarballs = list(dist_path.glob("*.tar.gz"))
    
    if not wheels:
        print("Error: No wheel file found")
        return False
    
    if not tarballs:
        print("Error: No source distribution found")
        return False
    
    print("\nBuilt packages:")
    for wheel in wheels:
        print(f"  - {wheel.name} ({wheel.stat().st_size // 1024} KB)")
    for tarball in tarballs:
        print(f"  - {tarball.name} ({tarball.stat().st_size // 1024} KB)")
    
    return True

def show_installation_instructions():
    """Show how to install the built package."""
    print("\n" + "=" * 60)
    print("Build Successful!")
    print("=" * 60)
    
    dist_path = Path("dist")
    wheels = list(dist_path.glob("*.whl"))
    
    if wheels:
        wheel_name = wheels[0].name
        print("\nTo install the package:")
        print(f"  pip install dist/{wheel_name}")
        
        print("\nTo install in editable mode (for development):")
        print("  pip install -e .")
        
        print("\nTo install with optional dependencies:")
        print("  pip install -e \".[tracing]\"     # With Opik tracing")
        print("  pip install -e \".[dev]\"         # With dev tools")
        print("  pip install -e \".[dev,tracing]\" # With everything")
        
        print("\nTo share with team:")
        print(f"  Copy dist/{wheel_name} to shared location")
        print(f"  Team installs: pip install {wheel_name}")
    
    print("\n" + "=" * 60)

def main():
    """Main build process."""
    print("=" * 60)
    print("Session Manager Library - Build Script")
    print("=" * 60)
    
    # Ensure we're in the right directory
    if not Path("pyproject.toml").exists():
        print("\nError: pyproject.toml not found!")
        print("Please run this script from the session_manager directory")
        return 1
    
    # Check/install build tools
    if not check_requirements():
        print("\nBuild tools not found. Installing...")
        if not install_build_tools():
            print("Failed to install build tools")
            return 1
        print("Build tools installed successfully")
    
    # Clean previous builds
    clean_previous_builds()
    
    # Build the package
    if not build_package():
        print("\nBuild failed!")
        return 1
    
    # Verify the build
    if not verify_build():
        print("\nBuild verification failed!")
        return 1
    
    # Show installation instructions
    show_installation_instructions()
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
