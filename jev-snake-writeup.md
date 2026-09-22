# I Taught a Model That Can't Write a Single Word to Play Snake

*What I learned putting TypeSafe's Jev, a "System One" classifier, in charge of a snake, and why the bugs were never where I expected.*

---

Most "AI plays a game" posts go like this: take a large language model, describe the screen, ask it what to do, parse its answer. I wanted to try something different.

[Jev](https://docs.typesafe.ai/introduction) is TypeSafe's first "System One" model. It is **not** a language model. It doesn't write text at all. You give it a *state* (some text describing a situation) and a set of *typed questions*, and it gives back structured answers with probabilities: pick one of these options, score this on a rubric, or say how likely this statement is to be true. The whole pitch is fast, calibrated gut-check decisions that your code can branch on directly, without parsing any prose.

A snake game is nothing but a stream of small, fast decisions. So I built one and let Jev steer.

> 🎥 *[Video: an 8×8 game from first move to crash. The bars under the board are Jev's probabilities for each direction, updating every move.]*

You can play it yourself with the arrow keys on a 6×6, 8×8, 10×10 or 20×20 board, or hit "Watch Jev play" and let the model take over.

## The setup

The game is a single HTML page with a canvas. A small Python server sits between the page and Jev. Each turn the page sends the board, and the server describes it in words, asks Jev, and returns a move.

I used [Pydantic AI's TypeSafe integration](https://pydantic.dev/docs/ai/models/typesafe/), which lets you point an ordinary `Agent` at Jev. The key idea is that **your output type is your list of questions**. Every field becomes one question, and its `description` is the wording:

```python
agent = Agent('typesafe:jev-latest', output_type=Look, instructions=GOAL)
result = agent.run_sync(observations)   # the prompt is only the material to judge
```

That flips the usual LLM habit. With an LLM you'd put the question and the material into one prompt. With Jev, the prompt is **only the situation**. The questions live on the type. If you write the question into the prompt, Jev treats it as just more text to judge.

## Version 1: I was doing all the thinking

My first attempt used a single pick-one question ("which way should the snake move?") with four options. For the state, I wrote something like:

```
Moving up: safe, gets closer to the apple, leaves plenty of room.
Moving down: the snake crashes into its own body and the game ends.
```

It worked, but it was a cheat. My code had already worked out which moves were safe and which got closer. Jev was just reading my conclusions back to me. That doesn't tell you anything about the model.

## Version 2: observations in, judgments out

TypeSafe's docs push one idea hard: **ask atomic questions and compose them in code.** Each question should be a judgment a knowledgeable person could make in a second. If a decision weighs several factors, ask each factor separately and combine them yourself.

So I split the state from the judgments. The state became only what the snake can *observe*:

```
The snake is 6 cells long and heading right.
The apple is 3 right and 2 up of the snake's head.
Above the head is an empty cell; 394 open cells can be reached from there.
Below the head is the snake's body, 3 cells back from the head.
Right of the head is the very tip of the snake's tail; 394 open cells can be reached from there.
```

The questions became three yes/no questions per direction, twelve in total, all sent in **one call**:

- Can the snake move *up* without hitting its own body?
- Does moving *up* bring the head closer to the apple?
- After moving *up*, is there enough open space to keep moving?

Each field is a `float` bounded between 0 and 1, which makes Jev return its raw probability of "yes" instead of a rounded True/False. My code then combines those probabilities into a score for each direction.

A call took about 0.2 to 0.5 seconds. The answers were sensible. One favourite: moving onto the *tip of the tail* got a "safe" probability of just 0.54. That's genuinely a tricky call, because the tail moves out of the way on the same step, so it *is* safe. Jev was honestly unsure, and it showed.

## Bug #1: the snake circled the apple forever

Then I watched it play, and the snake would get right next to the apple and... go around it. Again and again.

I assumed Jev was confused. Instead of guessing, I replayed games outside the browser and logged every probability on every turn. Here is a typical moment, with the apple directly below the head:

| Move | eats? | safe? | closer? | room? |
|---|---|---|---|---|
| **down (onto the apple)** | not asked yet | **0.54** | **0.94** | 0.61 |
| left | not asked yet | 0.90 | 0.13 | 0.87 |

Jev knew perfectly well that "down" was closer (0.94). But it rated stepping onto the apple as *less safe* than an empty cell. It was treating the apple as an obstacle. And my scoring formula multiplied by "safe" while weighting room above closeness, so the empty cell won every time.

The fix had three parts:

1. **Tell Jev the goal.** Agent `instructions` are shared framing sent with every question, so I used them: *"The goal is to eat the apple in as few moves as possible. The apple is food, not an obstacle: moving onto it is always safe."*
2. **Ask about the goal directly.** A fourth question per direction: *"Does moving {d} put the snake's head on the apple?"*
3. **Rebalance the scoring** so eating and getting closer actually drive the choice.

On the same 150-move replay: **5 apples → 12 apples**, and moves away from an apple within three cells went from **26 → 0**. On the 20×20 board, a 500-move game reached 45 apples without crashing once.

### One honest footnote: the safety net

The game checks Jev's top-scoring move against the real board. If it would crash and another move wouldn't, the game plays the best surviving move instead and says so on screen. It fires rarely, but it's there, and a write-up that didn't mention it would be overselling the model.

## Making it faster: speculative fan-out

Each answer took about 300 ms, and that time was spent on Jev's side. Reusing the connection didn't help. So I couldn't make a single answer faster. I could only stop waiting for one answer before asking the next question.

TypeSafe's docs describe a pattern called *Speculative Fan-Out*: send extra questions, including ones you might not need, and let your code decide which answers are relevant. I applied it across time:

- Along with the current board, cheap code *guesses* Jev's next two moves and builds the boards that would follow.
- All three boards go to Jev in parallel.
- A prefetched answer is used **only if Jev really picked the move that leads to that board**. Every move the snake plays is still Jev's own decision for that exact board. A wrong guess just gets thrown away.

| | Moves per round trip | Time per move |
|---|---|---|
| One call per move | 1.0 | 315 to 358 ms |
| Guessing 2 moves ahead | ~1.7 | 212 to 251 ms |

That's about 1.5× faster, for about 1.6 API calls per move. Guessing 4 to 6 moves ahead cost 40 to 70% more calls and wasn't any faster, so two was the sweet spot.

### The bug that wasn't about AI at all

Somewhere in here the game froze completely. No errors, no slow responses, just nothing. The cause had nothing to do with Jev: my Python server handled one connection at a time, and the browser opens spare connections ahead of time. One idle connection was blocking every request behind it. One-word fix, `ThreadingHTTPServer`. Worth remembering that when you put a model behind a server, most of the ways it can fail are still ordinary software failures.

## "Does it have state awareness?"

This was the most interesting question of the project, and the honest answer was **no, only what I tell it.**

Each move is an independent call. Jev has no memory of the last turn, where it's been, or how the body is shaped beyond the four cells next to its head. Its entire world is the paragraph I send.

So I tested it. On small boards, Jev never won. On 6×6 it lost 8 out of 8 games, usually after filling half to three-quarters of the board. The snake kept walking into pockets it had no way to see.

I gave it three new kinds of information:

1. **The whole board**, as a text grid (`H` head, `o` body, `t` tail, `A` apple).
2. **Escape routes**: for each move, whether the head could still reach its own tail afterwards, which is the classic test for a dead end.
3. **Recent history**: the last six moves, and how many moves since the last apple.

## Bug #2: Jev saw the trap, and my code walked into it anyway

The first result was a surprise: with more information, the snake did **worse** (20.2 vs 23.2 cells on 6×6).

Back to the logs. Jev's "enough room?" answers were excellent:

- Moves into a dead end: **0.09 to 0.18**
- Moves that kept an escape route: **0.62 to 0.77**

Jev had clearly spotted the trap. But my formula only let room *halve* a move's score, while a nearby apple could *triple* it. In 9 of the 17 moments where a dead end was one of the options, the apple won and the snake walked into the trap.

I made room a gate, just like safety:

```python
score = safe * roomy * (1 + 3 * eats + 2 * closer)
```

Then I reran the same games, adding each piece of information one at a time:

| What Jev sees | 6×6 (avg length / 36) | 8×8 (avg length / 64) |
|---|---|---|
| Neighbouring cells only | 26.4 | 36.2 |
| + recent moves | not run | 37.6 |
| + whole board | not run | 38.6 |
| + escape routes | not run | 39.4 |
| **All three** | **28.5** | **45.0** |

Any one addition helps a little. All three together help a lot: about 25% longer snakes on 8×8. (These are small samples, 5 to 8 games each, with individual games ranging from 27 to 52 cells. Trust the ranking more than the exact numbers.)

It still doesn't win. Filling the whole board means planning many moves ahead, and Jev is built to make fast judgments about *this* moment, not to plan. That's a real limit, not a bug.

## The last 10%: making it *feel* right

Watching Jev play, the snake moved in bursts, because each reply brought 1 to 3 moves at once. Two small changes fixed that:

- **A buffer and a steady beat.** Moves go into a small queue, and the snake plays one per tick. The tick changes by at most 1% per move, and only if the buffer is running low or piling up.
- **Pre-rolled apples.** Apple positions come from a pre-generated random sequence, so the page can predict the board even after an apple gets eaten, and keep asking Jev ahead of time.

Afterwards, consecutive moves differed by a median of **2 ms**. And all 65 boards Jev was asked about in advance matched the boards the game actually reached.

## What I'd tell someone trying this

**1. Put the situation in the state and the question on the type.** This is the biggest mental shift away from LLMs. The prompt is evidence, not instructions.

**2. Ask small questions and compose them in code, but know that the code is now half your model.** Both of my big bugs were in a one-line scoring formula, not in the model.

**3. When the model looks dumb, log the probabilities before changing anything.** Twice I was sure Jev was wrong. Twice the probabilities showed it had the right read and my code was overruling it. Calibrated outputs are only useful if you actually look at them.

**4. A stateless model knows exactly what you tell it.** "State awareness" isn't a property of the model; it's a property of the state you build. The fun part is testing which facts actually help.

**5. Be honest about who's doing the work.** My code still computes things like reachable space and tail reachability, then *describes* them to Jev. Jev turns those observations into judgments and my code turns the judgments into a move. Where that line sits is a design choice, and it's worth being upfront about it.

**6. Latency is a design problem, not just a number.** You can't make one call faster, but with speculative fan-out you can make it matter less.

---

*One more practical note: `jev-latest` moves when TypeSafe ship a release, which can shift the numbers under you. Once you've tuned thresholds and weights against your own data, pin the version you tuned against (`typesafe:jev-1.13.0`) and move deliberately.*

*The whole thing is about 700 lines: one HTML page for the game and a ~300-line Python server. The model is `jev-1.13.0`, called through Pydantic AI. I built this with the help of Claude Code.*
