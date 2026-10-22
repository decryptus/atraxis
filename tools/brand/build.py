#!/usr/bin/env python3
"""Rebuild Atraxis vector and raster brand assets from the approved artwork.

Run from any directory: python tools/brand/build.py
SVG exports contain paths, never embedded raster data or external fonts.
"""
from pathlib import Path
import io
import json
import tempfile
import re
import xml.etree.ElementTree as ET

import cairosvg
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import vtracer

ROOT = Path(__file__).resolve().parents[2]
BRAND = ROOT / 'assets/brand'
NAVY = '#031027'
BLUE = '#3284ed'
CYAN = '#46c9ee'
NS = '{http://www.w3.org/2000/svg}'


def trace(im, colors=40):
    rgb = im.convert('RGB')
    a = np.asarray(rgb)
    alpha = np.where(a.max(2) > 55, 255, 0).astype('uint8')
    rgba = rgb.quantize(colors=colors, method=Image.Quantize.MAXCOVERAGE).convert('RGBA')
    rgba.putalpha(Image.fromarray(alpha))
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td)/'input.png', Path(td)/'output.svg'
        rgba.save(src)
        vtracer.convert_image_to_svg_py(str(src), str(dst), colormode='color',
            hierarchical='stacked', mode='spline', filter_speckle=10,
            color_precision=6, layer_difference=24, corner_threshold=70,
            length_threshold=3.5, max_iterations=10, splice_threshold=45, path_precision=2)
        root = ET.parse(dst).getroot()
        return ''.join(ET.tostring(e, encoding='unicode').replace('ns0:', '').replace(':ns0','') for e in root)


def flat_word(im):
    a = np.asarray(im.convert('RGB'))
    on = a.max(2) > 70
    rgba = np.zeros((*on.shape, 4), dtype=np.uint8)
    rgba[on] = [50, 132, 237, 255]
    # Original X boundaries, relative to the cropped wordmark.
    region = rgba[:, 573:710]
    region[on[:, 573:710]] = [70, 201, 238, 255]
    return trace(Image.fromarray(rgba).convert('RGB'), colors=3)


def svg(body, w, h, title='Atraxis', bg=None):
    backdrop = f'<rect width="{w}" height="{h}" fill="{bg}"/>' if bg else ''
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-labelledby="title">'
            f'<title id="title">{title}</title>{backdrop}{body}</svg>')


def write_svg(name, body, w, h, bg=None):
    p = BRAND/'svg'/f'{name}.svg'
    p.write_text(svg(body,w,h,bg=bg), encoding='utf8')
    return p


def render(src, dst, width, bg=None):
    cairosvg.svg2png(url=str(src),write_to=str(dst),output_width=width,background_color=bg)


def main():
    for folder in ('svg','png','favicon','social','preview','pdf','webp'):
        (BRAND/folder).mkdir(parents=True,exist_ok=True)
    source = Image.open(BRAND/'source/atraxis-approved.png')
    # Crops retain approved artwork coordinates and letter shapes.
    master=ET.parse(BRAND/'source/atraxis-symbol-master.svg').getroot()
    shapes=''.join(ET.tostring(e,encoding='unicode').replace('ns0:', '').replace(':ns0','') for e in master)
    icon=f'<g transform="translate(-280 -220)">{shapes}</g>'
    word = flat_word(source.crop((175,842,1082,992)))
    sym = f'<g transform="translate(40 82)">{icon}</g>'
    stacked = f'<g transform="translate(180 35)">{icon}</g><g transform="translate(66 685)">{word}</g>'
    horizontal = f'<g transform="translate(20 15) scale(.48)">{icon}</g><g transform="translate(390 95)">{word}</g>'
    specs = {'atraxis-symbol':(sym,760,760),'atraxis-logo-stacked':(stacked,1040,875),
             'atraxis-logo-horizontal':(horizontal,1340,320),'atraxis-wordmark':(f'<g transform="translate(30 25)">{word}</g>',967,200)}
    for name,(body,w,h) in specs.items():
        path=write_svg(name,body,w,h)
        write_svg(name+'-dark',body,w,h,NAVY)
        for width in ([128,256,512,1024] if name.endswith('symbol') else [512,1024,2048]):
            render(path,BRAND/'png'/f'{name}-{width}.png',width)
        for bg,tag in [(NAVY,'dark'),('#ffffff','light')]:
            render(path,BRAND/'png'/f'{name}-{tag}-1024.png',1024,bg)

    # Simplified eye and three-node orbit for 16–48 px UI use.
    # Intentional optical variant: fewer facets and thicker orbit, same identity.
    compact = '''<path d="M22 10C44-3 68 15 63 42C62 49 53 57 43 58" fill="none" stroke="#59aafa" stroke-width="3"/>
<circle cx="43" cy="7" r="4" fill="#6aafff"/><circle cx="59" cy="20" r="4" fill="#6aafff"/><circle cx="60" cy="48" r="4" fill="#6aafff"/>
<path d="M3 34L31 13L59 34L31 56Z" fill="#237ce7"/>
<path d="M3 34L31 13L31 23Z" fill="#99d5ff"/><path d="M31 13L59 34L42 30Z" fill="#59aff9"/>
<path d="M3 34L31 56L24 44Z" fill="#195ec8"/><path d="M31 56L59 34L42 40Z" fill="#74c4ff"/>
<path d="M9 34Q31 12 53 34Q31 56 9 34Z" fill="#031027"/>
<circle cx="31" cy="34" r="13" fill="#2d8eea"/><circle cx="31" cy="34" r="6" fill="#031027"/>
<circle cx="28" cy="30" r="2.3" fill="#e0f7ff"/>'''
    small=write_svg('atraxis-symbol-small',f'<g transform="translate(4 4)">{compact}</g>',72,72)
    fav=BRAND/'favicon/favicon.svg'
    fav.write_text(svg(f'<g transform="translate(4 4)">{compact}</g>',72,72,bg=NAVY),encoding='utf8')
    for n in [16,32,48,64]:
        render(fav,BRAND/'favicon'/f'favicon-{n}x{n}.png',n)
    master=Image.open(io.BytesIO(cairosvg.svg2png(url=str(fav),output_width=256))).convert('RGBA')
    master.save(BRAND/'favicon/favicon.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
    # Maskable icon: entire mark within the central 80%-diameter safe circle.
    maskable=svg(f'<g transform="translate(132 147) scale(.365)">{icon}</g>',512,512,bg=NAVY)
    for n in (192,512):
        cairosvg.svg2png(bytestring=maskable.encode(),write_to=str(BRAND/'favicon'/f'android-chrome-{n}x{n}.png'),output_width=n)
    cairosvg.svg2png(bytestring=maskable.encode(),write_to=str(BRAND/'favicon/apple-touch-icon.png'),output_width=180)
    # Single-color cutout, simplified and stencil-safe; no raster dependencies.
    mono='''<path fill="currentColor" fill-rule="evenodd" d="M3 34L31 13L59 34L31 56ZM10 34Q31 15 52 34Q31 53 10 34Z"/>
<circle cx="31" cy="34" r="7" fill="currentColor"/>
<path d="M22 10C44-3 68 15 63 42C62 49 53 57 43 58" fill="none" stroke="currentColor" stroke-width="3"/>
<g fill="currentColor"><circle cx="43" cy="7" r="4"/><circle cx="59" cy="20" r="4"/><circle cx="60" cy="48" r="4"/></g>'''
    for color,label in [('#031027','navy'),('#ffffff','white')]:
        write_svg('atraxis-symbol-mono-'+label,f'<g transform="translate(4 4)" color="{color}">{mono}</g>',72,72)
        mw=re.sub(r'fill="[^"]+"',f'fill="{color}"',word)
        mb=f'<g transform="translate(180 80) scale(9)" color="{color}">{mono}</g><g transform="translate(66 685)">{mw}</g>'
        mp=write_svg('atraxis-logo-mono-'+label,mb,1040,875)
        render(mp,BRAND/'png'/f'atraxis-logo-mono-{label}-1024.png',1024)
    (BRAND/'favicon/safari-pinned-tab.svg').write_text(svg(f'<g transform="translate(4 4)" color="#000000">{mono}</g>',72,72),encoding='utf8')
    manifest={'name':'Atraxis','short_name':'Atraxis','icons':[{'src':f'android-chrome-{n}x{n}.png','sizes':f'{n}x{n}','type':'image/png','purpose':'any maskable'} for n in (192,512)],'theme_color':NAVY,'background_color':NAVY,'display':'standalone'}
    (BRAND/'favicon/site.webmanifest').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
    # Reusable social previews, no product functionality claims.
    # Wider Open Graph layout centers the horizontal signature.
    social=svg(f'<g transform="translate(40 180) scale(.835)">{horizontal}</g>',1200,630,bg=NAVY)
    cairosvg.svg2png(bytestring=social.encode(),write_to=str(BRAND/'social/atraxis-open-graph-1200x630.png'))
    render(BRAND/'svg/atraxis-symbol-dark.svg',BRAND/'social/atraxis-avatar-1024.png',1024)
    for name in ['atraxis-logo-stacked','atraxis-logo-horizontal']:
        cairosvg.svg2pdf(url=str(BRAND/'svg'/f'{name}.svg'),write_to=str(BRAND/'pdf'/f'{name}.pdf'))
        Image.open(BRAND/'png'/f'{name}-1024.png').save(BRAND/'webp'/f'{name}-1024.webp',lossless=True)
    preview()


def preview():
    sheet=Image.new('RGB',(1600,1300),'#e5eaf2')
    d=ImageDraw.Draw(sheet)
    try:
        font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',24)
    except OSError:
        font=ImageFont.load_default(size=24)
    tiles=[('Stacked / dark','svg/atraxis-logo-stacked.svg',(30,65,770,680),NAVY),
           ('Stacked / light','svg/atraxis-logo-stacked.svg',(830,65,770,680),'#ffffff'),
           ('Horizontal','svg/atraxis-logo-horizontal.svg',(30,795,1120,275),NAVY),
           ('Symbol','svg/atraxis-symbol.svg',(1190,795,370,370),NAVY)]
    for label,src,(x,y,w,h),bg in tiles:
        d.text((x,y-34),label,font=font,fill='#16223a')
        tile=Image.new('RGBA',(w,h),bg)
        raster=Image.open(io.BytesIO(cairosvg.svg2png(url=str(BRAND/src),output_width=w-40))).convert('RGBA')
        raster.thumbnail((w-40,h-40),Image.Resampling.LANCZOS)
        tile.alpha_composite(raster,((w-raster.width)//2,(h-raster.height)//2))
        sheet.paste(tile.convert('RGB'),(x,y))
    d.text((35,1130),'Favicons: 16 / 32 / 48 px (native size)',font=font,fill='#16223a')
    x=35
    for n in (16,32,48):
        im=Image.open(BRAND/'favicon'/f'favicon-{n}x{n}.png').convert('RGBA')
        sheet.paste(im,(x,1180),im); x+=n+30
    sheet.save(BRAND/'preview/atraxis-brand-overview.jpg',quality=92)


if __name__=='__main__':
    main()
