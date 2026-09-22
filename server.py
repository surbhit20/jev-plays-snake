# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic-ai-slim[typesafe]"]
# ///
"""Let Jev (TypeSafe's classifier) play snake.

Serves index.html and answers POST /jev with Jev's next move. Jev is not a language model:
the prompt is what the snake observes, and the questions live on the output type's fields.
See https://pydantic.dev/docs/ai/models/typesafe/ and https://docs.typesafe.ai/introduction

    uv run server.py            # then open http://localhost:8000

The API key is read from TYPESAFE_API_KEY, or from a .env file next to this script.
"""

import asyncio
import json
import os
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

# Load KEY=value lines from .env next to this file; a variable already set in the shell wins.
_env = Path(__file__).parent / '.env'
if _env.exists():
    for line in _env.read_text().splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key and not key.startswith('#'):
            os.environ.setdefault(key.strip(), value.strip().strip('\'"'))

from pydantic import Field, create_model
from pydantic_ai import Agent

MODEL = os.environ.get('JEV_MODEL', 'typesafe:jev-latest')
PORT = int(os.environ.get('PORT', 8000))
# How many moves ahead to guess and ask Jev about in parallel (Speculative Fan-Out).
SPECULATE = int(os.environ.get('JEV_SPECULATE', 2))
# What Jev sees beyond the head's neighbours: any of board, escape, history ('full' is all, 'basic' none).
STATE = os.environ.get('JEV_STATE', 'full')
EXTRAS = {'full': {'board', 'escape', 'history'}, 'basic': set()}.get(STATE, set(STATE.split(',')))
HERE = Path(__file__).parent
DIRS = {'up': (0, -1), 'down': (0, 1), 'left': (-1, 0), 'right': (1, 0)}
HISTORY = 6  # how many recent moves Jev is told about
PLACE = {'up': 'Above', 'down': 'Below', 'left': 'Left of', 'right': 'Right of'}
OPPOSITE = {'up': 'down', 'down': 'up', 'left': 'right', 'right': 'left'}


# Atomic questions composed in code (https://docs.typesafe.ai/patterns): Jev gets observations,
# not conclusions, and answers four yes/no questions per direction in one call. A float bounded
# 0..1 comes back as Jev's probability of yes, which the scoring below combines.
QUESTIONS = {
    'eats': 'Does moving {d} put the snake\'s head on the apple?',
    'safe': 'Can the snake move {d} without hitting its own body?',
    'closer': 'Does moving {d} bring the snake\'s head closer to the apple?',
    'roomy': 'After moving {d}, is there enough open space for the whole snake to keep moving?',
}
GOAL = (
    'The goal is to eat the apple in as few moves as possible. The apple is food, not an obstacle: '
    'moving onto it is always safe, and the snake grows by one cell.'
)

Look = create_model(
    'Look',
    __doc__='Steer a snake straight to the apple without it hitting its own body.',
    **{
        f'{d}_{q}': (float, Field(ge=0, le=1, description=text.format(d=d)))
        for d in DIRS
        for q, text in QUESTIONS.items()
    },
)

agent = Agent(MODEL, output_type=Look, instructions=GOAL, defer_model_check=True)


def observe(state: dict) -> tuple[str, dict[str, dict]]:
    """What the snake can see this turn, and the ground truth the game uses to check Jev."""
    n = state['n']
    snake = [tuple(p) for p in state['snake']]
    food = tuple(state['food'])
    heading = state['dir']
    head = snake[0]
    index = {p: i for i, p in enumerate(snake)}

    def wrap_delta(a: int, b: int) -> int:
        d = (b - a) % n
        return d - n if d > n // 2 else d

    dx, dy = wrap_delta(head[0], food[0]), wrap_delta(head[1], food[1])
    where = ' and '.join(
        s
        for s in (
            f'{abs(dx)} {"right" if dx > 0 else "left"}' if dx else '',
            f'{abs(dy)} {"down" if dy > 0 else "up"}' if dy else '',
        )
        if s
    )
    lines = [
        f'The board is {n} by {n} cells and its edges wrap around.',
        f'The snake is {len(snake)} cells long and heading {heading}. Every cell of its body except the '
        'very tip of its tail stays put for the next move.',
        f'The apple is {where} of the snake\'s head.',
    ]
    truth: dict[str, dict] = {}
    for name, (mx, my) in DIRS.items():
        nxt = ((head[0] + mx) % n, (head[1] + my) % n)
        eats = nxt == food
        i = index.get(nxt)
        if eats:
            seen = 'the apple'
        elif i is None:
            seen = 'an empty cell'
        elif i == 1:
            seen = "the snake's neck"
        elif i == len(snake) - 1:
            seen = "the very tip of the snake's tail"
        else:
            seen = f"the snake's body, {i} cells back from the head"
        body = snake if eats else snake[:-1]  # the tail moves away unless the snake grows
        crash = nxt in body
        room = 0 if crash else flood(nxt, set(body) | {nxt}, n)
        escape = not crash and reaches_tail([nxt, *body], n)
        after = f'; {room} open cells can be reached from there' if not crash else ''
        if 'escape' in EXTRAS and not crash:
            after += (
                ', and the snake could still follow its own tail from there'
                if escape
                else ', but the snake could no longer reach its own tail from there, so it may be a dead end'
            )
        lines.append(f'{PLACE[name]} the head is {seen}{after}.')
        truth[name] = {'legal': name != OPPOSITE[heading], 'crash': crash, 'eats': eats, 'escape': escape}
    if 'board' in EXTRAS:
        lines += board_lines(n, snake, food)
    if 'history' in EXTRAS:
        history = state.get('history') or []
        if history:
            lines.append(f'The snake\'s last {len(history)} moves, oldest first: {", ".join(history)}.')
        if 'since_apple' in state:
            lines.append(f'It has been {state["since_apple"]} moves since the snake last ate an apple.')
    return '\n'.join(lines), truth


def board_lines(n: int, snake: list, food: tuple) -> list[str]:
    """The whole board as a grid, top row first."""
    grid = [['.'] * n for _ in range(n)]
    for x, y in snake[1:-1]:
        grid[y][x] = 'o'
    grid[snake[-1][1]][snake[-1][0]] = 't'
    grid[food[1]][food[0]] = 'A'
    grid[snake[0][1]][snake[0][0]] = 'H'
    return [
        'The whole board, top row first: H is the head, o the body, t the tip of the tail, A the apple, . empty.',
        *(' '.join(row) for row in grid),
    ]


def reaches_tail(snake: list, n: int) -> bool:
    """Whether the head has a path to the tail tip, the classic test for not being boxed in."""
    head, tail = snake[0], snake[-1]
    blocked = set(snake[1:-1])
    seen, queue = {head}, deque([head])
    while queue:
        x, y = queue.popleft()
        for dx, dy in DIRS.values():
            p = ((x + dx) % n, (y + dy) % n)
            if p == tail:
                return True
            if p not in seen and p not in blocked:
                seen.add(p)
                queue.append(p)
    return False


def flood(start: tuple[int, int], blocked: set, n: int) -> int:
    seen, queue = {start}, deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in DIRS.values():
            p = ((x + dx) % n, (y + dy) % n)
            if p not in seen and p not in blocked:
                seen.add(p)
                queue.append(p)
    return len(seen) - 1


def advance(state: dict, move: str) -> dict | None:
    """The board after `move`, or None when it eats (the next apple is unknown) or crashes."""
    n, snake = state['n'], [tuple(p) for p in state['snake']]
    dx, dy = DIRS[move]
    head = ((snake[0][0] + dx) % n, (snake[0][1] + dy) % n)
    if head == tuple(state['food']) or head in snake[:-1]:
        return None
    history = [*(state.get('history') or []), move][-HISTORY:]
    since = state.get('since_apple', 0) + 1
    return {**state, 'snake': [head, *snake[:-1]], 'dir': move, 'history': history, 'since_apple': since}


def guess(state: dict) -> str:
    """A cheap guess at Jev's pick, only used to decide which future boards to prefetch."""
    n, head, food = state['n'], state['snake'][0], state['food']
    _, truth = observe(state)

    def dist(move: str) -> int:
        dx, dy = DIRS[move]
        x, y = (head[0] + dx) % n, (head[1] + dy) % n
        return min((x - food[0]) % n, (food[0] - x) % n) + min((y - food[1]) % n, (food[1] - y) % n)

    moves = [d for d in DIRS if truth[d]['legal'] and not truth[d]['crash']] or [state['dir']]
    return min(moves, key=lambda d: (dist(d), d != state['dir']))


async def plan(state: dict) -> dict:
    """Jev's moves for this board and, when the guesses hold, the next few boards too.

    Every returned move is Jev's own pick for the exact board it is played on: a speculative
    board is only used if the move leading to it is the one Jev actually chose.
    """
    chain = [state]
    while len(chain) <= SPECULATE:
        nxt = advance(chain[-1], guess(chain[-1]))
        if nxt is None:
            break
        chain.append(nxt)
    answers = await asyncio.gather(*(ask_jev(s) for s in chain))
    steps = [answers[0]]
    for board, answer in zip(chain[1:], answers[1:]):
        if board['dir'] != steps[-1]['move']:
            break
        steps.append(answer)
    return {'steps': steps, 'calls': len(chain)}


async def ask_jev(state: dict) -> dict:
    observations, truth = observe(state)
    result = await agent.run(observations)
    answers = result.output.model_dump()
    rows = {}
    for d in DIRS:
        p = {q: answers[f'{d}_{q}'] for q in QUESTIONS}
        # Surviving the next step and not boxing itself in both gate the move; the goal (eat,
        # else get closer) only chooses among moves Jev thinks are safe and roomy.
        score = p['safe'] * p['roomy'] * (1 + 3 * p['eats'] + 2 * p['closer'])
        rows[d] = {**p, 'score': round(score, 3) if truth[d]['legal'] else None}
    legal = [d for d in DIRS if truth[d]['legal']]
    pick = max(legal, key=lambda d: rows[d]['score'])
    move, overridden = pick, False
    if truth[pick]['crash']:
        # Jev rated a crash as the best move: take its best-scored move that survives, and say so.
        survivors = [d for d in legal if not truth[d]['crash']]
        if survivors:
            move, overridden = max(survivors, key=lambda d: rows[d]['score']), True
    return {
        'move': move,
        'jev_pick': pick,
        'overridden': overridden,
        'rows': rows,
        'model': result.response.model_name,
        'observations': observations,
    }


# One long-lived event loop for all requests, so the parallel calls share one client.
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/', '/index.html'):
            return self.send_error(404)
        self._send(200, (HERE / 'index.html').read_bytes(), 'text/html; charset=utf-8')

    def do_POST(self):
        if self.path != '/jev':
            return self.send_error(404)
        try:
            state = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            body, status = asyncio.run_coroutine_threadsafe(plan(state), LOOP).result(), 200
        except Exception as e:  # surface the reason to the page instead of a bare 500
            body, status = {'error': f'{type(e).__name__}: {e}'}, 500
        self._send(status, json.dumps(body).encode(), 'application/json')

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page reloaded or closed while Jev was answering

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    if not os.environ.get('TYPESAFE_API_KEY'):
        sys.exit('Set TYPESAFE_API_KEY, or put TYPESAFE_API_KEY=... in .env (keys: https://console.typesafe.ai/).')
    print(f'Jev ({MODEL}) is ready to play at http://localhost:{PORT}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
