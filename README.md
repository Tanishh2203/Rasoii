# Rasoi — The Complete Story

> A voice-first assistant for Swiggy that lets people order food, buy groceries, or convert any recipe into a grocery cart — by talking, typing, or sending a photo. Built on Swiggy's MCP platform. Telegram bot in v1, multi-platform in v2.

---

## 1. The Problem and the Solution

**Problem:** 600+ million Indians use messaging apps daily but only a fraction comfortably navigate food and grocery apps. The friction is real — too many menus, too many filters, too much typing, and crucially, no voice. Even when people figure out how to order, the bot or app feels mechanical: ask, get reply, ask again, get reply. No memory, no proactive help.

**Solution:** Rasoi. Open Telegram, hold the mic, say *"mujhe pyaaz aur tamatar mangwana hai ghar pe"* — your groceries arrive in 15 minutes. Send a screenshot of a recipe — Rasoi extracts the ingredients and builds the cart. Paste a YouTube cooking URL — same thing, faster. Place an order and the bot automatically pings you with delivery updates. Say "phir se wahi mangwa do" and it pulls your past orders.

Same Swiggy backend, same prices, same delivery. Just a fundamentally different interface — one that listens, watches, remembers, and proactively helps.

---

## 2. Who Is This Actually For?

Let me be honest, because "everyone benefits" is marketing nonsense.

### Primary target

**Tech-comfortable but app-fatigued adults (30–55).** People who live on WhatsApp/Telegram, prefer voice notes to typing, find modern apps cluttered. Doctors between patients, teachers between classes, small-business owners who don't want to scroll through 200 restaurant tiles. They know how to order on Swiggy — they just don't want to.

**Multilingual users (Hindi, Hinglish, English).** The Swiggy app forces English UI for many flows. Rasoi accepts whatever language you naturally speak and replies in the same.

**People with hands full.** Cooking and want groceries delivered before you finish? Speak into the phone without putting down the knife. Driving (as passenger), feeding a baby, exercising — anywhere typing is awkward.

**Repeat-order users.** "I want my usual" used to need 6 taps. Now it's 4 words.

### Secondary target

**Older relatives (55–75) who use WhatsApp but not Swiggy.** This is the accessibility story. More below.

**Visually impaired users.** Voice-in + voice-out reduces the screen-reading burden dramatically.

### Not the target

- Tech-savvy young users → they prefer the actual Swiggy app with filters and photos
- Feature-phone users → need a smartphone with Telegram
- Users with zero Swiggy account → still need to log in once via OAuth

---

## 3. The Older-People Question 

### Where Rasoi genuinely helps older users

1. **No new app to learn.** If they use WhatsApp or Telegram (most over-50s now do), the interface is *already familiar*. Same chat bubbles, same voice button.

2. **Voice-first.** Typing on a small screen is brutal at 65. Holding the mic and saying *"sabzi mangwa do"* is dramatically easier.

3. **Replies in their language.** Speak Hindi → bot speaks Hindi back (Roman script with English-accented voice; not perfect but understandable). No translation cognitive load.

4. **Conversational.** No "tap here → swipe there → choose filter." Just say what you want and refine.

5. **Confirmation gates.** Bot always says "Confirm this cart of 3 items for 120 rupees?" before any order is placed. Reduces accidental orders — a real fear for older users.

6. **Cash on Delivery only.** No UPI PIN, no card details, no payment failure debugging. Cash arrives, food/groceries arrive.

7. **Proactive delivery updates.** Bot tells them "your order is 2 min away" without them having to check or remember anything. Hugely valuable when memory and patience are both finite resources.

8. **Reorder by description.** "Same as last week's vegetables" pulls history. No need to remember what they ordered or how to find it.

### Where Rasoi does NOT magically solve elderly tech struggles

1. **First-time setup needs help.** Someone has to install Telegram, find the bot, and run the OAuth login once. One-time hurdle but real.

2. **Smartphone literacy is still required.** They need to know how to hold the mic button, open a chat. If they've never opened WhatsApp, Rasoi won't help.

3. **TTS accent is American.** Comprehensible but clearly foreign-accented Hindi. Not "talking to an Indian assistant." More like "Siri trying Hindi."

4. **Speech recognition errors.** Whisper handles standard Hindi well but heavy regional accents lose ~10% accuracy. Have to re-record sometimes.

5. **Trust takes time.** Older users won't trust an AI to order correctly until they've watched it succeed multiple times.

**Realistic picture:** Rasoi is a meaningful improvement for older people *who have someone in their family to do the one-time setup*. Once that's done, the day-to-day use is genuinely easier than the Swiggy app. But it's not a solution for digitally-excluded seniors with no support system — that requires offline channels (phone-order services).

---

## 4. What We Actually Built

Eight distinct capabilities, each independently useful:

| Capability | How user invokes it | What happens |
|------------|--------------------|--------------|
| **Text chat → food/groceries** | Type into Telegram | Agent searches Swiggy, builds cart, confirms, orders |
| **Voice chat → food/groceries** | Hold mic, speak (Hindi/English/Hinglish) | Whisper transcribes; agent processes; voice-out for short replies |
| **Recipe image → groceries** | Send a recipe screenshot or photo | Claude vision extracts ingredients; pantry-staple aware; builds Instamart cart |
| **YouTube cooking video → groceries** | Paste a YouTube URL | Fetches transcript; extracts ingredients; same flow as image |
| **Auto-routing (supervisor)** | Just say what you want | Classifier picks Food vs Instamart silently — no mode commands needed |
| **Auto order tracking** | Happens after every checkout | Bot polls order status every minute and pushes updates to your chat |
| **Reorder my usual** | "Reorder my last", "phir se wahi mangwa do" | Pulls order history, offers to repeat one |
| **Mode override** | `/mode food`, `/mode instamart` | Manual override of supervisor (useful for edge cases) |

All capabilities use the same Swiggy MCP servers, the same conversation memory, the same confirmation-before-order flow. They're composable — you can speak a recipe, get auto-tracked, then say "reorder my usual" via voice for the next meal.

---

## 5. How It Works — In Plain Language

A complete walkthrough of saying *"mujhe ek kilo pyaaz chahiye"* into Telegram.

### The seven layers

**Layer 1 — Telegram (the messaging surface)**
What you see. Telegram passes your voice note to the bot's server. It doesn't know about food — it's just a pipe.

**Layer 2 — Whisper (the ears)**
Your voice note is sent to OpenAI's Whisper service. Whisper converts audio to text in Hindi, English, or mixed. The audio becomes: *"mujhe ek kilo pyaaz chahiye"*.

**Layer 3 — The Router (supervisor classifier)**
A tiny LLM call looks at "mujhe ek kilo pyaaz chahiye" and decides: this is grocery intent, not food. Routes to Instamart brain. If the request had been ambiguous ("can I get something hot?"), the router defaults to whichever mode you're already in.

**Layer 4 — The Brain (Claude)**
Claude reads the message, knows it's an Instamart request, decides what tools to call. It has been told:
- Reply in Roman Hindi if user spoke Hindi (for TTS quality)
- Always confirm before placing an order
- Always Cash on Delivery
- The user is in Noida
- If the user says "reorder", look up history first
- And ~12 other rules

Claude calls these tools in sequence:
1. `get_addresses` → finds your Noida address
2. `search_products` → finds onions at your location
3. `update_cart` → adds 1kg onions
4. Asks you to confirm

**Layer 5 — The Mouth (OpenAI TTS)**
Claude's reply ("Theek hai, 1 kg pyaaz 33 rupees mein mil gaya. Add karu cart mein?") is short. Your input was voice. So instead of texting back, the bot sends the reply as a spoken voice note. You *hear* the answer.

**Layer 6 — You confirm**
You say *"haan kar de"* (voice note again).
Whisper transcribes it.
Router decides "stay in Instamart mode" (this is a continuation).
Brain calls `checkout`.
Real order placed.
Bot says (in voice): "Order placed. ETA 12 minutes."

**Layer 7 — The Background Helper (order tracker)**
The moment `checkout` succeeds, a background task spawns. It quietly polls the order status every 60 seconds. While you're doing other things, it sends you:

```
📦 Order update: Order confirmed, preparing
📦 Order update: Order picked up by delivery partner
📦 Order update: Delivery partner 1 min away
📦 Order update: Delivered
```

Then it stops polling. No code on your part. No need to ask.

---

### Visual summary

```
You: [voice note] "mujhe ek kilo pyaaz chahiye"
                       │
       ┌───────────────┴────────────────┐
       │                                │
       ▼                                ▼
   Layer 1: Telegram delivers audio    
       │
       ▼
   Layer 2: Whisper → "mujhe ek kilo pyaaz chahiye"
       │
       ▼
   Layer 3: Router → "this is grocery" → switch to Instamart mode silently
       │
       ▼
   Layer 4: Brain calls Swiggy MCP tools:
            • get_addresses → your Noida home
            • search_products("onions") → Onion 1kg ₹33
            • update_cart → added
       │
       │ Generates reply: "1 kg pyaaz 33 rupees. Add karu?"
       ▼
   Layer 5: Reply is short + you spoke → speak the reply via TTS
       │
       ▼
   You: [voice note] "haan kar de"
       │
       ▼
   Brain calls checkout → real order placed
   Bot speaks: "Order placed. ETA 12 minutes."
       │
       │ (Tools called: ["update_cart", "checkout"])
       ▼
   Layer 7: Helper sees "checkout" was called
            Looks up latest order ID
            Spawns background tracker task
       │
       ▼
   While you do other things:
   ┌────────────────────────────────────┐
   │  Every 60s, helper polls track_order  │
   │  Sends YOU push notifications when     │
   │  status changes                        │
   └────────────────────────────────────┘
       │
       ▼
   12 minutes later, real Instamart delivery person knocks on door
```

---

## 6. The Technical Journey — What We Built And When

Seven phases, ~2.5 weeks, each building on the previous.

### Phase 1 — OAuth + first MCP call (Day 1)

**What:** Script that logs into Swiggy via OAuth and calls `get_addresses`. Outputs your saved addresses.

**Why it mattered:** Validated authentication and connection before building anything fancier.

**Components:** Dynamic Client Registration, PKCE OAuth 2.1, Streamable HTTP MCP transport.

### Phase 2 — Conversational CLI agent (Days 2–3)

**What:** Terminal-based agent. Type messages, agent calls Swiggy MCP tools, builds carts, confirms before ordering.

**Why it mattered:** Validated the agent loop (user → Claude → tool → result → Claude → reply) without UI complications.

**Real test:** Placed an actual ₹50 onion order via terminal. Architecture validated.

### Phase 3 — Recipe parser (Days 4–6)

**What:** Two scripts:
- `parse_image.py` — sends image to Claude vision, extracts ingredients
- `parse_youtube.py` — fetches YouTube transcript, extracts ingredients

Plus `cook_this.py` — combines parser with agent: image/URL in, grocery cart out.

**Why it mattered:** The "magical" capability that makes Rasoi different from generic chatbots.

**Hard problem:** Recipe ingredient names ≠ Instamart SKU names. Solved with pantry-staple detection, quantity normalization.

### Phase 4 — (Skipped) Dineout

Not built. Focused on Food + Instamart for tight scope.

### Phase 5 — Telegram bot (Days 7–9)

**What:** Wrapped agent in a Telegram bot. Per-user session, image+text+URL input, allowlist for security.

**Why it mattered:** Made the project usable by anyone with Telegram.

**Engineering:** Refactored agent loop from `while True` script into stateless `process_message()` for multi-user readiness.

### Phase 6 — Voice in + voice out (Days 10–11)

**What:** Whisper for voice-to-text input. OpenAI TTS for spoken replies. Voice-out triggers when input was voice AND reply is short.

**Why it mattered:** Voice-only ordering is qualitatively different from text. Demo moment.

**Smart touches:** When user speaks, Claude is told to be brief AND reply in Roman Hindi (Hinglish) for better TTS pronunciation.

### Phase 7 — Supervisor + tracking + history (Days 12–14) **[NEW]**

This phase is where the product crossed from "demo-able" to "feels like a real assistant."

**What got built:**

**A) Supervisor classifier (auto-routing).** Before this, users had to type `/mode food` or `/mode instamart` to switch contexts. Now a tiny Claude Haiku call before each message decides: is this food intent, grocery intent, or a continuation? Routes accordingly. Conservative — defaults to "stay in current mode" for ambiguous messages so confirmations and follow-ups don't cause weird mode flips.

**B) Auto order tracking.** Before this, you'd place an order and have to ask "track my order" to get updates. Now, after every successful `checkout` or `place_food_order`, the bot:
1. Looks up the latest order ID via `get_orders` / `get_food_orders`
2. Spawns a background `asyncio.Task`
3. The task polls `track_order` / `track_food_order` every 60 seconds
4. On status changes, pushes Telegram messages directly to the user
5. Stops on terminal states ("delivered", "cancelled") or after 90 minutes

This is the change that makes the bot feel alive. You place an order, look at your phone 10 minutes later, see a notification: "📦 Delivery partner 2 min away."

**C) Reorder awareness.** Both system prompts updated: when user says "reorder", "same as last time", "my usual", "phir se mangwa do", the agent calls `get_food_orders` / `get_orders` first to look up history, then offers to repeat a recent order. No new code — just smarter prompting.

**Why this phase matters most:** It's the difference between "a clever tool" and "an assistant I'd actually use daily." Auto-routing removes friction. Auto-tracking removes anxiety. Reorder awareness removes repetition. Together they're the three things that separate a prototype from a product.

---

## 7. Technical Choices And Why We Made Them

### Why Claude (Anthropic)?
- Native MCP support, designed to work with it cleanly
- Reliable tool-calling — fewer hallucinations matter when real money is involved
- Strong multilingual handling for Hindi/Hinglish
- Familiar — built agent systems with this stack at Wellwiz

### Why Claude Haiku 4.5 (not Sonnet)?
- ~10x cheaper for the same workload
- Fast — agent conversations involve many round trips; latency adds up
- Tool selection is structured work, not deep reasoning — Haiku handles it
- The classifier especially benefits from Haiku speed (~200ms per call vs Sonnet's 800ms)

### Why per-message classifier instead of one big agent with all 35 tools?
- The "all 35 tools at once" approach causes attention degradation in LLMs — they confuse Food vs Instamart contexts and hallucinate cart IDs (documented by other Builders Club builders)
- Classifier-first keeps each agent focused with only its own server's tools
- The classifier is fast and cheap (~₹0.01/call)
- Trade-off: tiny extra latency before each turn. Imperceptible in practice.

### Why background asyncio for tracking instead of webhooks?
- Swiggy MCP doesn't provide webhooks (it's a pull-based protocol)
- Polling is the only option
- 60-second interval balances responsiveness with API politeness
- asyncio tasks survive across conversation turns without blocking new messages

### Why Telegram instead of WhatsApp?
- WhatsApp Business requires Facebook Business account + Meta verification (hit dead ends)
- Twilio sandbox doesn't reliably deliver to Indian numbers
- Telegram bot: 3 minutes via @BotFather, no business verification
- For a developer-facing demo (Swiggy engineers), Telegram is *more* credible — shows technical competence not corporate paperwork

### Why OpenAI Whisper API?
- ~₹0.50/min, 1-2 second latency, zero setup
- Local Whisper would be ~10-20 second latency on a MacBook Air CPU
- Demo latency is everything

### Why OpenAI TTS (not Sarvam)?
- Same API key as Whisper (one less integration)
- Quality is acceptable for English; English-accented for Roman-script Hindi
- Sarvam has native Hindi voices and is on the v2 roadmap

### Why JSON files for storage?
- Single-user demo, no concurrency
- One readable file per user (`data/histories/<user_id>.json`)
- Zero operational overhead
- Production swap: SQLite or Postgres

### Why Cash on Delivery only?
- Swiggy MCP v1 only supports COD — platform constraint
- Reframed positively: COD is safer for older users (no UPI PIN dance, no card-not-present fraud)

### Why conservative supervisor classifier?
- "Stay in current mode" is the default when uncertain
- Prevents weird mid-conversation jumps (confirmations like "yes" shouldn't trigger a mode switch)
- The trade-off is occasional mis-routing for genuinely ambiguous queries — solved by manual `/mode` override

---

## 8. What Rasoi Does Really Well

1. **One-message ordering with no mode setup.** "mujhe pyaaz chahiye" → done. Supervisor routes, brain orders, tracker watches. You don't think about which app or which mode.

2. **Recipe-to-cart in under 10 seconds.** From "I want to make this" (with a screenshot) to a built cart.

3. **Multi-language without switching modes.** Speak Hindi, get Hindi. Speak English, get English. Mix them — works.

4. **Confirms before mutating.** Every order goes through "Confirm this for X rupees?" No accidental orders.

5. **Real Swiggy integration.** Not a simulation. Real orders placed, real food delivered.

6. **Lives in the background.** After you place an order, the bot proactively keeps you informed. No checking required.

7. **Remembers your past orders.** "Reorder my usual" works without any user setup or training data.

8. **Architecturally clean.** Each component (parser, agent, voice, classifier, tracker) is independently testable. Swap any piece without breaking others.

9. **Smart about staples.** Recipe says "salt"? Bot asks if you already have it. Skips it if yes.

---

## 9. What It Doesn't Do (Honest Limitations)

1. **Single user per bot instance.** Multi-user OAuth flow not implemented (Phase 5b). Each user needs their own bot for now.

2. **No order editing after placement.** Swiggy MCP doesn't support cancellation.

3. **No scheduled delivery.** Platform v1 limitation.

4. **TTS accent isn't Indian.** OpenAI voices sound American. Comprehensible but not native. Sarvam swap is the v2 fix.

5. **No payment except COD.** Platform constraint.

6. **Order ID extraction relies on regex.** If Swiggy changes their order ID format, the tracker won't start. Easily fixable with one regex update.

7. **Tracker terminal-state detection is keyword-based.** If Swiggy uses unusual status text, the tracker may keep polling unnecessarily (harmless but wasteful).

8. **Mode switch clears history.** When supervisor switches from food to instamart mid-conversation, food cart context is lost. By design but worth knowing.

9. **Classifier mis-routes occasionally.** ~5% of ambiguous queries get wrong mode. Manual `/mode` override fixes it.

10. **Voice recognition fails on heavy regional accents.** ~10% error rate.

11. **YouTube transcripts unavailable for ~20% of cooking videos.**

12. **No image-of-existing-product reorder.** "Get me what's in this photo" isn't supported. Only recipe images.

---

## 10. Money And Effort Reality

**Development cost (everything):**
- Anthropic API: ~₹600 during development (includes classifier overhead)
- OpenAI API (Whisper + TTS): ~₹200
- Telegram, ngrok, hosting: ₹0 (free tiers)
- Real Swiggy orders for testing: ~₹500

**Total: under ₹1500 for the entire build.**

**Per-order operational cost (production):**
- Classifier (Haiku): ~₹0.05 per message
- Main agent (Haiku): ~₹4 per order conversation
- Whisper: ~₹0.50 per voice note
- TTS: ~₹0.30 per spoken reply
- Tracker: ~₹2 per order (polls cost API calls)
- Telegram: free

**~₹12 per order** in API costs. Healthy margin at scale.

**Time investment:** ~14 days, mostly evenings.

---

## 11. The Demo / Application Story

Built specifically for Swiggy Builders Club. What this demonstrates:

- **Real MCP integration, multi-server orchestration** (Food + Instamart with auto-routing)
- **India-first use case** (Hindi/Hinglish, WhatsApp/Telegram-native, designed for actual Indian market)
- **Production-thinking** (confirmation flows, cart limits, error handling, allowlists, background tasks)
- **Working voice agent** (in and out)
- **Recipe parsing as a unique angle** (most submissions will be chatbots)
- **Proactive behavior** (auto-tracking demonstrates async architecture competence)
- **Memory + history awareness** (shows understanding of stateful agents)

---

## 12. What's Next (Roadmap)

**Immediate (post-Swiggy approval):**
- Multi-user OAuth via ngrok callback (Phase 5b)
- WhatsApp Business migration after Meta verification
- Sarvam AI TTS for native Hindi voices
- Dineout server integration

**Medium-term:**
- Group ordering (multi-user cart pooling)
- Diet/allergen profile (refuses unsafe items)
- Preference learning (favorite restaurants, usual brands)
- Image-of-dish → similar restaurant suggestions

**Longer-term:**
- Phone IVR (call a number, talk, order — no smartphone needed)
- Calendar integration (auto-suggest lunch before meetings)
- Regional language voices (Tamil, Bengali, Marathi)

---

## 13. Honest Reflection — What I'd Do Differently

If starting over:

1. **Skip the WhatsApp detour.** Two days lost trying Twilio + Meta before switching to Telegram. Should've gone Telegram from day one.

2. **Build supervisor earlier.** Manual `/mode` switching was always clunky. The classifier-based supervisor took 4 hours to write and would've been worth doing in Phase 5.

3. **Start with `auto+log` recipe matching** instead of "ask every time." Demo lag from per-ingredient questions is painful.

4. **Add observability sooner.** Logging came late.

5. **Test with real older users.** I theorized about who benefits without putting it in front of someone over 60. Real user testing is the next step.

6. **Build tracking first, voice second.** Order tracking is the feature that makes the product feel alive. Voice is the demo moment. In retrospect, tracking should've come right after Phase 5.

---

## 14. Tech Stack Summary

| Layer | Technology | Why |
|-------|-----------|-----|
| **Messaging** | Telegram (python-telegram-bot v21) | Free, instant setup, polling not webhook |
| **Speech-to-text** | OpenAI Whisper API | Fast, Hindi-capable, no model download |
| **Reasoning** | Claude Haiku 4.5 (Anthropic SDK) | Cheap, fast, reliable tool-calling, native MCP |
| **Supervisor classifier** | Claude Haiku 4.5 (same key) | Tiny per-message routing call, ~200ms |
| **Tool calling** | Swiggy MCP servers (Food + Instamart) | Real Swiggy backend, official protocol |
| **MCP client** | Python `mcp` SDK with streamable HTTP | Standard, well-maintained |
| **Recipe vision** | Claude Haiku (vision) | Same API key, all image formats |
| **YouTube parsing** | `youtube-transcript-api` v1.x | Free, no API key |
| **Text-to-speech** | OpenAI TTS-1 (`nova` voice) | Same API key as Whisper |
| **Background tracking** | Python asyncio.Task | Native async, survives across turns |
| **Storage** | JSON files per user | Simple, debuggable, sufficient for v1 |
| **OAuth** | Swiggy's DCR + PKCE | Standard, no manual provisioning |
| **Language** | Python 3.10+ async | Required by python-telegram-bot v21+ |

---

## 15. Final Note

This is a working prototype shipped in ~14 days by one person. Every flow works end-to-end. Real orders have been placed and tracked. The architecture supports growth without rewrite.

The progression has been deliberate: prove the OAuth, prove the agent, prove the parser, ship the bot, add voice, then add the three "feels real" features (auto-routing, auto-tracking, history awareness). Each phase validated the previous.

If Swiggy's Builders Club is looking for projects demonstrating real engineering on their MCP platform with an India-first user story, Rasoi qualifies. If you're an older user who's struggled with food apps, or a busy professional tired of menus, this is the interface I'd want for you.

Code: [your GitHub URL]
Demo video: [your Loom URL]
Contact: [your email]
