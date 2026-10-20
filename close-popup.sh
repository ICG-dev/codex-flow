#!/bin/sh
# Codex notify: cierra el popup de tmux al terminar el turno principal.
# Ignora el turno interno que genera el titulo de la tarea.
case "$1" in
  *'"type":"agent-turn-complete"'*)
    case "$1" in *'single-line task title'*) exit 0 ;; esac
    tmux display-popup -C ;;
esac
