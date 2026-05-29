#!/bin/bash
websockify --cert /etc/ssl/novnc.cert --key /etc/ssl/novnc.key --web=/opt/noVNC/ $PORT localhost:5900
