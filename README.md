# compose-guard

A small deployment watcher for one Docker Compose server. It remembers the current image, applies the requested image through a separate override file, watches container state and an optional HTTP URL, and restores the previous image after repeated failed checks.

## Install

```sh
sudo install -m 0755 compose_guard.py /usr/local/bin/compose-guard
```

Run from the directory containing `compose.yml`:

```sh
compose-guard deploy --service web --image example/web:2.4 --watch 30m --check https://example.com/health
```

For a detached run, for example:

```sh
systemd-run --unit=compose-guard-web --collect compose-guard deploy --service web --image example/web:2.4 --watch 30m
```

Optional notifications use `--notify-telegram` with `CG_TELEGRAM_TOKEN` and `CG_TELEGRAM_CHAT` in the environment, and/or `--notify-webhook https://hooks.example.net/deploy`. Never pass the Telegram token as a command-line argument. Use `compose-guard status` to inspect the saved result and `compose-guard rollback --service web` for a manual rollback.

## Limitations

Requires Python 3.10+, Docker Compose v2, and a Unix system with `fcntl`. Only a single service image is overridden. The prior image must be known from a running Compose container. URL checks accept any 2xx response; there is no TLS pinning. The watchdog is a foreground process, so run it under a service manager for unattended deployments.

## Русский

`compose-guard` следит за выкладкой одного сервиса Docker Compose и возвращает прежний образ после нескольких неудачных проверок. Запускай его из каталога с compose-файлом и используй менеджер служб для фонового наблюдения. Команда `status` показывает результат, `rollback` запускает ручной откат.
