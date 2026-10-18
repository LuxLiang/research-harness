"""Render the illustrative README workflow. Requires Pillow 11.1 (authoring only).

Run from any directory: python scripts/render_workflow_animation.py
Install the authoring dependency with: python -m pip install Pillow==11.1.0
Outputs: 22-second, 30 fps, 2400x1360 lossless transparent animated PNG and still.
Artwork is rendered at 3x and exported at 2x logical resolution.
Fonts use DejaVu, available in most Linux distributions. Override --font-dir
when rendering elsewhere. No research execution or evidence is represented.
"""
from pathlib import Path
import argparse
import io
import math
import struct
import zlib

from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, H, SCALE, OUTPUT_SCALE, FPS, SECONDS = 1200, 680, 3, 2, 30, 22
BG, INK, MUTED, LINE = (0, 0, 0, 0), '#222D32', '#6E7777', '#D7DDD8'
TEAL, BLUE, RED = '#247B6B', '#52749B', '#B25C48'
STAGES = [
    ('Question', 'Start with a question.', ['Turn a research objective into a plan,', 'with explicit claims and evidence needs.'], 'Objective → plan → claims'),
    ('Research', 'Develop the evidence.', ['Theory and experiments follow the plan.', 'Each output keeps its source revision.'], 'Theory + experiments'),
    ('Verify', 'Put claims under scrutiny.', ['Independent reviews examine the evidence.', 'Formal claims also take the Lean path.'], 'Independent checks'),
    ('Revise', 'Let failure change the work.', ['A failed check sends the claim back.', 'Revise, then verify the new evidence.'], 'Revise → verify again'),
    ('Approve', 'Keep judgment human.', ['Verified claims feed the research draft.', 'Final approval stays with the researcher.'], 'Draft → human approval'),
]


class AnimationWriter:
    """Stream lossless APNG delta rectangles with full alpha replacement."""

    def __init__(self, stream, count):
        self.stream, self.count, self.sequence = stream, count, 0
        self.previous = None
        stream.write(b'\x89PNG\r\n\x1a\n')

    def chunk(self, kind, payload):
        self.stream.write(struct.pack('>I', len(payload)) + kind + payload
                          + struct.pack('>I', zlib.crc32(kind + payload)))

    def add(self, frame, duration):
        first = self.previous is None
        bounds = (0, 0, *frame.size) if first else ImageChops.difference(
            frame, self.previous).getbbox(alpha_only=False)
        bounds = bounds or (0, 0, 1, 1)
        cropped = frame.crop(bounds)
        encoded = io.BytesIO()
        cropped.save(encoded, format='PNG', compress_level=9)
        data = encoded.getvalue()
        if first:
            self.chunk(b'IHDR', struct.pack('>IIBBBBB', *frame.size, 8, 6, 0, 0, 0))
            self.chunk(b'acTL', struct.pack('>II', self.count, 0))
        self.chunk(b'fcTL', struct.pack('>IIIIIHHBB', self.sequence,
                   *cropped.size, bounds[0], bounds[1], duration, 1000, 0, 0))
        self.sequence += 1
        offset = 8
        while offset < len(data):
            length = struct.unpack('>I', data[offset:offset+4])[0]
            if data[offset+4:offset+8] == b'IDAT':
                payload = data[offset+8:offset+8+length]
                if first:
                    self.chunk(b'IDAT', payload)
                else:
                    self.chunk(b'fdAT', struct.pack('>I', self.sequence) + payload)
                    self.sequence += 1
            offset += length+12
        self.previous = frame.copy()

    def finish(self):
        self.chunk(b'IEND', b'')


def render_base(t, fonts):
    image = Image.new('RGBA', (W*SCALE, H*SCALE), BG)
    d = ImageDraw.Draw(image)
    stage = min(int(t / 4.4), 4)
    local = (t % 4.4) / 4.4
    active_paths = []

    def text(x, y, value, size=16, color=INK, face='sans'):
        d.text((x*SCALE, y*SCALE), value, font=fonts[face, size], fill=color)

    def line(points, color=LINE, width=1):
        d.line([(x*SCALE, y*SCALE) for x, y in points], fill=color, width=width*SCALE, joint='curve')

    def box(x, y, w, h, fill=BG, outline=LINE, radius=12):
        d.rounded_rectangle((x*SCALE, y*SCALE, (x+w)*SCALE, (y+h)*SCALE), radius=radius*SCALE, fill=fill, outline=outline, width=SCALE)

    def route(points, active=False, color=TEAL):
        line(points, color if active else LINE, 2 if active else 1)
        if active:
            active_paths.append((points, color))

    def card(x, y, title, detail, active=False, color=TEAL, width=174):
        box(x, y, width, 78, BG, color if active else LINE)
        text(x+15, y+15, title, 15 if len(title) > 16 else 17, color if active else MUTED)
        text(x+15, y+43, detail, 12, MUTED, 'mono')

    text(44, 28, 'R / H', 16, TEAL, 'mono')
    text(124, 26, 'RESEARCH HARNESS', 16)
    text(923, 30, 'ILLUSTRATIVE WORKFLOW', 12, MUTED, 'mono')
    line([(44, 64), (1156, 64)])
    text(44, 88, 'From research questions to verifiable claims.', 32, INK, 'serif')
    text(45, 139, 'Structured research. Independent checks. Traceable evidence.', 16, MUTED)
    line([(815, 195), (815, 548)])

    # Stable topology makes the return path easy to follow.
    route([(125, 304), (125, 241), (254, 241)], stage == 0)
    route([(428, 241), (459, 241), (459, 224), (492, 224)], stage == 1, BLUE)
    route([(428, 241), (459, 241), (459, 349), (492, 349)], stage == 1, BLUE)
    route([(666, 224), (750, 224), (750, 431), (668, 431)], stage == 2)
    route([(666, 349), (716, 349), (716, 431), (668, 431)], stage == 2)
    route([(494, 431), (428, 431)], stage in (2, 3), RED if stage == 3 and local < .52 else TEAL)
    if stage == 3:
        route([(341, 392), (341, 299)], True, RED if local < .52 else TEAL)
        text(254, 352, 'revise + recheck', 12, RED if local < .52 else TEAL, 'mono')
    route([(254, 431), (212, 431)], stage == 4)

    card(45, 304, 'Question', 'research objective', stage == 0, width=160)
    card(254, 202, 'Research plan', 'scope + evidence', stage <= 1)
    card(492, 185, 'Theory', 'Lean when required', stage in (1, 2), BLUE)
    card(492, 310, 'Experiment', 'pinned code + data', stage in (1, 2), BLUE)
    card(494, 392, 'Independent review', 'checks + alignment', stage in (2, 3), width=174)

    failed = stage == 3 and local < .52
    verified = stage == 4 or (stage == 3 and local >= .72)
    claim_color = RED if failed else TEAL
    box(254, 392, 174, 94, '#FBF0EB' if failed else '#EEF4EF', claim_color)
    text(270, 405, 'Scientific claim', 17, claim_color)
    state = 'Revision needed' if failed else 'Verified' if verified else 'Under review' if stage >= 2 else 'Proposed'
    text(270, 438, state, 12, claim_color, 'mono')
    card(45, 392, 'Research draft', 'human approval', stage == 4, width=167)
    if stage == 4:
        box(46, 495, 165, 27, '#F4EBDD', '#E2D2B7', 5)
        text(56, 500, 'Awaiting approval', 12, '#8B6631', 'mono')

    number = f'0{stage+1}'
    name, title, body, label = STAGES[stage]
    text(851, 199, number + ' / ' + name.upper(), 13, TEAL, 'mono')
    # Keep the editorial heading readable at README display size.
    headings = [['Start with', 'a question.'], ['Develop', 'the evidence.'], ['Put claims', 'under scrutiny.'], ['Let failure', 'change the work.'], ['Keep judgment', 'human.']]
    for i, value in enumerate(headings[stage]):
        text(850, 236+i*42, value, 30, INK, 'serif')
    for i, value in enumerate(body):
        text(851, 349+i*26, value, 15, MUTED)
    box(851, 430, 303, 49, '#EDF2ED', '#D9E4DA', 6)
    text(863, 446, label, 13, TEAL, 'mono')
    text(851, 511, 'Evidence stays linked to revisions.', 13, MUTED)

    line([(44, 568), (1156, 568)])
    for i, (name, *_) in enumerate(STAGES):
        x = 44+i*226
        line([(x, 589), (x+205, 589)], LINE, 3)
        if i <= stage:
            line([(x, 589), (x+205, 589)], TEAL, 3)
        text(x, 605, f'0{i+1}  {name}', 15, TEAL if i == stage else MUTED)
    text(44, 648, 'Agents propose. The controller checks. Humans approve.', 12, MUTED)
    text(868, 648, 'Concept illustration · not a live run', 12, MUTED)
    return image.resize((W*OUTPUT_SCALE, H*OUTPUT_SCALE), Image.Resampling.LANCZOS), active_paths


BASE_FRAMES = {}


def render(t, fonts):
    stage = min(int(t / 4.4), 4)
    local = (t % 4.4) / 4.4
    phase = 0 if stage != 3 or local < .52 else 1 if local < .72 else 2
    key = (stage, phase)
    if key not in BASE_FRAMES:
        representative = stage*4.4 + (1 if phase == 0 else 2.6 if phase == 1 else 3.5)
        BASE_FRAMES[key] = render_base(representative, fonts)
    base, paths = BASE_FRAMES[key]
    image = base.copy()
    if paths:
        # One moving marker keeps the current route legible and lets APNG
        # store small changed rectangles instead of repeating the whole diagram.
        points, color = paths[0]
        lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
        distance = (t*.65 % 1)*sum(lengths)
        for a, b, length in zip(points, points[1:], lengths):
            if distance <= length:
                u = distance / length
                x, y = a[0]+u*(b[0]-a[0]), a[1]+u*(b[1]-a[1])
                left, top = math.floor(x-6), math.floor(y-6)
                marker = Image.new('RGBA', (12*SCALE, 12*SCALE))
                ImageDraw.Draw(marker).ellipse(((x-left-4)*SCALE, (y-top-4)*SCALE,
                    (x-left+4)*SCALE, (y-top+4)*SCALE), fill=color)
                marker = marker.resize((12*OUTPUT_SCALE, 12*OUTPUT_SCALE), Image.Resampling.LANCZOS)
                image.alpha_composite(marker, (left*OUTPUT_SCALE, top*OUTPUT_SCALE))
                break
            distance -= length
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font-dir', type=Path, default=Path('/usr/share/fonts/truetype/dejavu'))
    args = parser.parse_args()
    faces = {'sans': 'DejaVuSans.ttf', 'serif': 'DejaVuSerif.ttf', 'mono': 'DejaVuSansMono.ttf'}
    fonts = {(face, size): ImageFont.truetype(str(args.font_dir / file), size*SCALE)
             for face, file in faces.items() for size in (12, 13, 15, 16, 17, 30, 32)}
    output = ROOT / 'docs/assets'
    output.mkdir(parents=True, exist_ok=True)
    # Delta rectangles retain exact RGBA pixels without buffering ~9 GB.
    count = FPS*SECONDS
    with (output / 'research-workflow.png').open('wb') as stream:
        encoder = AnimationWriter(stream, count)
        for i in range(count):
            frame = render(i/FPS, fonts)
            if i == 8*FPS:
                frame.save(output / 'research-workflow-still.png')
            encoder.add(frame, round((i+1)*1000/FPS)-round(i*1000/FPS))
            if i % FPS == 0:
                print(f'Rendered {i}/{count} frames', flush=True)
        encoder.finish()
    print(f'Rendered {count} lossless RGBA frames to {output}')


if __name__ == '__main__':
    main()
