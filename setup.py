from setuptools import setup


APP = ['FlexSpotBridge.py']
DATA_FILES = []
OPTIONS = {
    'packages': ['tkinter'],
    'includes': [],
    'excludes': [],
    'iconfile': 'FlexSpotBridge.icns',
    'plist': {
        'CFBundleName': 'FlexSpotBridge',
        'CFBundleDisplayName': 'FlexSpotBridge',
        'CFBundleIdentifier': 'com.yourdomain.FlexSpotBridge',
        'CFBundleShortVersionString': '1.1.0',
        'CFBundleVersion': '1',
        'CFBundleGetInfoString': 'FlexSpotBridge 1.1.0-beta.1',
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)