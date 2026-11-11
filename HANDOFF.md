# HANDOFF — Agent Guard (второй проект на NEAR HACKATHON)

_Обновлено: 2026-11-12_

## Что это
Платёжный файрвол между ИИ-агентами и деньгами. Трек: AI Security & Defense (+ Agent Economy).
Агент не держит ключей, он только просит платёж через API. Guard: правила → ИИ-скринер (Gemini) → allow / hold / block →
выплата из казны (USDC на NEAR напрямую, другие сети через 1Click) → журнал с хэш-цепочкой.
Первый проект: AI-Штат Pay (github.com/Dengel88/nearhackaton). README обоих должны ссылаться друг на друга (в этом уже есть).

## Состояние
- Код: FastAPI, `app/` (policy, screen, guard, near, store, agent, main), страницы `web/index.html` (демо), `web/console.html` (владелец).
- 18 тестов зелёные: `pytest -q`. Mock-режим работает без ключей.
- Проверено вживую из облака: Gemini gemini-3.8-flash (агент + скринер), 1Click live-котировки USDC NEAR → Base/Solana.
- NEAR-подпись своя (pynacl + borsh), байт-в-байт совпадает с py-near-primitives. Живую отправку не проверяли: из облака нет доступа к NEAR RPC и нет ключа казны.

## Нужно от Сэма
- [ ] Vercel: импортировать репо как новый проект. Env: LLM_PROVIDER=gemini, GEMINI_API_KEY, ONECLICK_MODE=live, EXECUTION_MODE=mock (пока), TURSO_DATABASE_URL/TURSO_AUTH_TOKEN (можно новую базу в Turso).
- [ ] Для живых выплат в видео: отдельный NEAR-аккаунт казны, ~$5 USDC + 0.1 NEAR, приватный ключ только в env Vercel (TREASURY_NEAR_ACCOUNT, TREASURY_PRIVATE_KEY, EXECUTION_MODE=live, OWNER_TOKEN латиницей).

## Дальше
- [ ] Деплой, проверка на проде, скриншоты.
- [ ] Реальная выплата $1 с казны (видео).
- [ ] Видео-демо, пост в X (@NEARProtocol #NEARHackathon), форма до 14.11 23:59 UTC.
- [ ] Идея-усиление: жёсткие лимиты on-chain (контракт или function-call key с allowance).
