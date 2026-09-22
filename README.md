# Jev plays Snake

A snake game steered by [Jev](https://docs.typesafe.ai/introduction), TypeSafe's "System One" classifier. It doesn't generate text at all. Every turn, the board is described to Jev in plain sentences, Jev answers 16 typed yes/no questions with probabilities, and a few lines of Python turn those probabilities into a move.

You can also just play it yourself with the arrow keys.

> 🎥 *[Video or GIF of a game goes here]*

## How it works

```
Browser (index.html)                  Server (server.py)                     TypeSafe
────────────────────                  ──────────────────                     ────────
board state ──POST /jev──────────▶  plan()
 (snake, apple, heading,               ├─ guess() the next 2 moves
  last moves, since apple)             ├─ build up to 3 boards
                                       ├─ for each board, in parallel:
                                       │    observe() → text description
                                       │    agent.run(text) ────────────▶  Jev answers 16
                                       │                                    questions, each
                                       │    score each direction  ◀──────  a probability 0 to 1
                                       └─ keep moves while guesses held
moves buffer ◀───── {steps: [...]} ───
play one per steady tick
```

**The state** is what the snake can observe: the board as a text grid, what's in each neighbouring cell, how much open space each one leads to, whether the snake could still reach its own tail from there, where the apple is, and the last few moves.

**The questions** live on the output type, one per field. For each direction: does it eat the apple, is it safe, does it get closer, is there enough room. Each is a `float` bounded 0 to 1, so Jev returns its probability rather than a rounded yes/no.

**The choice** is made in code, not by the model:

```python
score = safe * roomy * (1 + 3 * eats + 2 * closer)
```

Safety and room act as gates; eating and closing the distance pick among what's left.

## Running it

Requires Python 3.10+ and a TypeSafe API key ([console.typesafe.ai](https://console.typesafe.ai/)).

```bash
echo "TYPESAFE_API_KEY=your-key" > .env
uv run server.py
```

Then open http://localhost:8000, choose a board size and click **Watch Jev play**.

Without [uv](https://docs.astral.sh/uv/): `pip install "pydantic-ai-slim[typesafe]"` and `python server.py`.

## Settings

Environment variables, or lines in `.env`:

| Variable | Default | What it does |
|---|---|---|
| `TYPESAFE_API_KEY` | none | Required. |
| `JEV_MODEL` | `typesafe:jev-latest` | Pin a version with e.g. `typesafe:jev-1.13.0`. |
| `JEV_STATE` | `full` | What Jev sees: `full`, `basic`, or any of `board,escape,history`. |
| `JEV_SPECULATE` | `2` | How many moves ahead to guess and prefetch. `0` disables it. |
| `PORT` | `8000` | Server port. |

## What it's like

- **It plays well, but it doesn't win.** On an 8×8 board it typically fills about 45 of 64 cells before trapping itself. Filling the whole board takes planning several moves ahead, which isn't what this kind of model does.
- **The state is everything.** Jev has no memory between moves. On 8×8, giving it the full board, escape routes and recent history got the snake about 25% longer than neighbouring cells alone.
- **Speed.** Each call takes roughly 300 ms, all of it on the model's side. Guessing two moves ahead and asking in parallel ([Speculative Fan-Out](https://docs.typesafe.ai/patterns/fan-out)) brings that down to about 210 to 250 ms per move, at roughly 1.6 calls per move.
- **A safety net.** If Jev's top-scoring move would crash and another wouldn't, the game plays the best surviving move instead and says so on screen. It fires rarely.

## Files

| File | What's in it |
|---|---|
| `index.html` | The game: canvas, controls, Jev's probability panel, and the steady-tick playback of Jev's moves. |
| `server.py` | Describes the board, asks Jev, scores the directions, prefetches ahead, serves the page. |

Built with [Pydantic AI](https://pydantic.dev/docs/ai/models/typesafe/).
