#!/bin/bash

cd /home/scott/Development/Other/Musibisk/
source /home/scott/Development/Other/Musibisk/venv/bin/activate

rm -rf /home/scott/Development/Other/Musibisk/dist
rm -rf /home/scott/Development/Other/Musibisk/build

pyinstaller Musibisk.spec
