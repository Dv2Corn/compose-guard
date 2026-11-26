# compose-guard

Deploy a new image to one Docker Compose server and get the old one back automatically if the new one breaks.

You run one command instead of `docker compose up -d`. For the next 30 minutes compose-guard checks that
the container keeps running, is not `unhealthy`, does not restart in a loop and (optionally) that a URL
answers 2xx. Three failed checks in a row and it puts the previous image back and tells you in Telegram.
One Python file, standard library only.

It remembers the current image, applies the requested image through a separate override file, watches container state and an optional HTTP URL, and restores the previous image after repeated failed checks.

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
systemd-run --unit=compose-guard-web --collect compose-guard deploy --file /srv/app/compose.yml --service web --image example/web:2.4 --watch 30m
```

Exit codes: `0` the new image held for the whole watch, `1` it failed and was rolled back, `2` usage or Docker error.

**Important:** the new image lives in `.compose-guard-override.yml`, your `compose.yml` is never edited.
A later plain `docker compose up -d` therefore starts the image written in `compose.yml` again. After a
successful deploy either update the tag in `compose.yml`, or always run Compose with both files:
`docker compose -f compose.yml -f .compose-guard-override.yml up -d`.

Optional notifications use `--notify-telegram` with `CG_TELEGRAM_TOKEN` and `CG_TELEGRAM_CHAT` in the environment, and/or `--notify-webhook https://hooks.example.net/deploy`. Never pass the Telegram token as a command-line argument. Use `compose-guard status` to inspect the saved result and `compose-guard rollback --service web` for a manual rollback.

## Limitations

Requires Python 3.10+, Docker Compose v2, and a Unix system with `fcntl`. Only a single service image is overridden. The prior image must be known from a running Compose container. URL checks accept any 2xx response; there is no TLS pinning. The watchdog is a foreground process, so run it under a service manager for unattended deployments.

## Русский

`compose-guard` следит за выкладкой одного сервиса Docker Compose и возвращает прежний образ после нескольких неудачных проверок. Запускай его из каталога с compose-файлом и используй менеджер служб для фонового наблюдения. Команда `status` показывает результат, `rollback` запускает ручной откат.
