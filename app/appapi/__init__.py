"""JSON API for the native «Диалоги» mobile app (Expo/React Native).

Mirrors what the web admin «Диалоги» does — list conversations, read a thread,
reply as an operator, pause/resume the bot, mark read — but as clean JSON with
stateless bearer-token auth (no cookies), plus Expo push-token registration so
the app gets native notifications on new client messages.
"""
