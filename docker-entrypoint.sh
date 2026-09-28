#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
    if [ ! -f "$PICMANAGE_HOME/index.db" ]; then
        python run.py db init
    fi
    set -- python run.py serve --host 0.0.0.0 --port 8000 --no-browser
fi

exec "$@"
