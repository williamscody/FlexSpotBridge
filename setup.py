from setuptools import setup
from version import APP_NAME, app_version_label


APP = ['FlexSpotBridge.py']
DATA_FILES = []
OPTIONS = {
    'packages': ['tkinter'],
    'includes': [],
    'excludes': [],
    'iconfile': 'FlexSpotBridge.icns',
    'plist': {
        'CFBundleName': APP_NAME,
        'CFBundleDisplayName': APP_NAME,
        'CFBundleIdentifier': 'com.yourdomain.FlexSpotBridge',
        'CFBundleShortVersionString': app_version_label(),
        'CFBundleVersion': '2',
        'CFBundleGetInfoString': f'{APP_NAME} {app_version_label()}',
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)