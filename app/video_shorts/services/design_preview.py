"""Live preview geometry. Returns numbers and text only, never rendered frames."""
from PIL import ImageFont
from app.video_shorts.config import DESIGN_GEOMETRY as D, SUBTITLE_PRESETS
from app.video_shorts.services.compositor import _title_overlay_layout
from app.video_shorts.services.transcript_service import _caption_frame_layout, _layout_wrapped_caption


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else [0, 0, 1, 1]


def _box(box, px, py, width, height):
    return [max(0, box[0]-px), max(0, box[1]-py),
            min(width, box[2]+px), min(height, box[3]+py)]


def measure_design(data, *, title_font_path, caption_font_path, title_behavior):
    width, height = int(data['width']), int(data['height'])
    title_size = int(data['title_size'])
    caption_size = max(1, int(round(int(data['caption_size']) * D['font_scale'])))
    karaoke = data.get('caption_style', 'karaoke') == 'karaoke'
    preset = (SUBTITLE_PRESETS.get(data.get('preset')) or {}) if karaoke else {}
    title = _title_overlay_layout(
        data.get('title') or ' ', font_path=str(title_font_path), font_size=title_size,
        target_width=width, target_height=height, base_y=int(data['title_top']),
        line_spacing=int(data.get('line_spacing', -4)), title_text_color=data.get('title_text_color'),
        subtitle_preset=data.get('preset'), uppercase=bool(title_behavior['title_uppercase']),
        video_top_limit=None, title_language=data.get('language'),
        title_bg_alpha=data.get('title_bg_alpha'),
        title_outline_color=title_behavior['title_outline_color'],
        title_outline_width=title_behavior['title_outline_width'],
    ) if str(data.get('title') or '').strip() else None
    title_result = dict(box=[0, 0, 1, 1], runs=[], lines=0, font_size=title_size)
    if title:
        font = title['font']
        boxes, runs = [], []
        for entry in title['entries']:
            x, y, text = entry['x'], entry['y'], entry['text']
            b = font.getbbox(text, anchor='la', stroke_width=title['outline_width'])
            boxes.append([x+b[0], y+b[1], x+b[2], y+b[3]])
            runs.append(dict(text=text, x=x, y=y+font.getmetrics()[0], size=title['font_size']))
        px = max(D['title_padding_min_x'], round(title['font_size']*D['title_padding_x']))
        py = max(D['title_padding_min_y'], round(title['font_size']*D['title_padding_y']))
        title_result = dict(box=_box(_union(boxes), px, py, width, height), runs=runs,
                            lines=len(runs), font_size=title['font_size'],
                            outline=title['outline_width'], outline_color=title['outline_color'],
                            anchor=title['draw_y'], shadow=title['shadow'])
    font = ImageFont.truetype(str(caption_font_path), caption_size)
    active_size = max(1, caption_size + int(preset.get('active_font_size_delta') or 0))
    active_font = ImageFont.truetype(str(caption_font_path), active_size)
    source_words = str(data.get('caption_source') or data.get('caption') or '').split()
    group_size = D['caption_words'] if karaoke else 10
    groups = [source_words[index:index+group_size] for index in range(0, len(source_words), group_size)]
    def group_score(group):
        best = (0, 0, 0)
        best_active = 0
        active_indexes = range(len(group)) if karaoke else (0,)
        for candidate_active in active_indexes:
            candidate = _caption_frame_layout(group, active_index=candidate_active, font=font,
                active_font=active_font, caption_language=data.get('language', ''),
                subtitle_margin=int(data['caption_margin']), font_size=caption_size,
                outline_width=int(preset.get('outline_width') or 0),
                force_uppercase=bool(preset.get('force_uppercase')),
                canvas_width=width, canvas_height=height, caption_anchor=data.get('caption_anchor', 'bottom'))
            widths = [int(line.get('width') or 0) for line in candidate['lines']]
            score = (len(candidate['lines']), max(widths or [0]), candidate['total_height'])
            if score > best:
                best, best_active = score, candidate_active
        return best, best_active
    selected_group = max(((group_score(group), group) for group in groups), key=lambda item: item[0][0]) if groups else None
    words = selected_group[1] if selected_group else []
    if not words:
        return dict(title=title_result, caption=dict(box=[0, 0, 1, 1], runs=[],
                    lines=0, font_size=caption_size, outline=0), width=width, height=height)
    active = selected_group[0][1] if selected_group and karaoke else min(max(0, int(data.get('active_word', 0))), max(0, len(words)-1))
    outline = int(preset.get('outline_width') or 0)
    draw_pill = bool(preset.get('active_pill')) and bool(preset.get('draw_pill', True))
    precomputed = None
    if not draw_pill and active_size == caption_size:
        display = [w.upper() if preset.get('force_uppercase') else w for w in words]
        precomputed = _layout_wrapped_caption(display, font=font,
            subtitle_margin=int(data['caption_margin']), max_width=width-2*D['caption_side_margin'],
            canvas_width=width, canvas_height=height, caption_anchor=data.get('caption_anchor', 'bottom'))
    layout = _caption_frame_layout(words, active_index=active, font=font, active_font=active_font,
        caption_language=data.get('language', ''), subtitle_margin=int(data['caption_margin']),
        font_size=caption_size, outline_width=outline,
        force_uppercase=bool(preset.get('force_uppercase')), precomputed_layout=precomputed,
        canvas_width=width, canvas_height=height, caption_anchor=data.get('caption_anchor', 'bottom'))
    runs, line_boxes, glyph_boxes = [], [], []
    pill = None
    for word in layout['words']:
        is_active = word['global_index'] == active
        f = active_font if is_active else font
        x, y = word['x'], word['y']
        b = f.getbbox(word['word'], anchor='la', stroke_width=outline)
        glyph_boxes.append([x+b[0],y+b[1],x+b[2],y+b[3]])
        runs.append(dict(text=word['word'], x=x, y=y+f.getmetrics()[0],
                         size=active_size if is_active else caption_size, active=is_active and karaoke))
        if is_active and draw_pill:
            py = max(D['pill_padding_min_y'], round(caption_size*D['pill_padding_y']))
            ph = b[3]-b[1]+2*py
            pw = max(word['slot_width'], round(ph*D['pill_min_aspect']))
            px = round(word['slot_x'] + word['slot_width']/2-pw/2)
            pill = [px, y+b[1]-py, px+pw, y+b[1]-py+ph]
    for line in layout['lines']:
        if line['words']:
            line_boxes.append([line['x'],line['y'],line['x']+line['width'],line['y']+layout['line_height']])
    px = max(D['caption_padding_min_x'], round(caption_size*D['caption_padding_x']))
    py = max(D['caption_padding_min_y'], round(caption_size*D['caption_padding_y']))
    caption_box = _box(_union(line_boxes), px, py, width, height)
    return dict(title=title_result, caption=dict(box=caption_box, runs=runs, pill=pill,
        glyph_box=_union(glyph_boxes), lines=len(layout['lines']), font_size=caption_size,
        outline=outline, active_outline=bool(preset.get('active_outline')),
        anchor=layout['lines'][0]['y'] if layout['lines'] else 0,
        block_height=layout['total_height'], text=' '.join(words)), width=width, height=height)
