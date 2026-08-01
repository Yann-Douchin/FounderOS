# Gallery capture checklist

## Build and launch

```bash
npm ci --prefix web
npm run build
npm start
```

Open `http://127.0.0.1:8080`, then run one scenario in another terminal:

```bash
python3 apps/founderos.py --demo --scenario linear_blocker
```

## Required captures

Capture each stable state with the emulator's PNG button:

1. `linear_blocker`, critical red accent and Linear source color
2. `calendar`, near-term commitment with time label
3. `gmail`, action-required email
4. `slack`, launch approval mention
5. `clear`, calm green idle state
6. agent permission request, pulsing timeout with red `NON` and green `OUI`
7. ChatGPT/Codex usage, one or two live quota bars

The emulator exports a 720×160 `preview.png`, exactly 10 display pixels per LED.

## V1 capture set

![Five FounderOS display states](captures/founderos-gallery.png)

- [Linear blocker, 720×160](captures/linear-blocker.png)
- [Calendar, 720×160](captures/calendar.png)
- [Gmail, 720×160](captures/gmail.png)
- [Slack, 720×160](captures/slack.png)
- [All clear, 720×160](captures/clear.png)
- [Agent permission, 720×160](captures/agent-permission.png)
- [ChatGPT/Codex usage, 720×160](captures/agent-usage.png)

## Review bar

Before publishing, verify:

- the first frame is understandable without waiting for scrolling;
- no text clips outside the 72×16 display;
- only one decision is visible;
- source, urgency, and action state are distinguishable without decorative noise;
- HTTP 409 leaves the higher-priority app undisturbed;
- no credentials or real message content appear in screenshots;
- `npm test` and `npm run build` pass from a clean checkout.
