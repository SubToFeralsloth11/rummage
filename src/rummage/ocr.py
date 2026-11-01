"""On-device OCR with Apple's Vision framework (no network, no extra models)."""

from __future__ import annotations

import objc
import Quartz
import Vision
from Foundation import NSURL


def _recognize(handler) -> str:
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    ok, _err = handler.performRequests_error_([req], None)
    if not ok:
        return ""
    lines = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if cands and len(cands):
            lines.append(str(cands[0].string()))
    return "\n".join(lines)


def ocr_image(path: str) -> str:
    with objc.autorelease_pool():
        url = NSURL.fileURLWithPath_(path)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        return _recognize(handler)


def ocr_pdf(path: str, max_pages: int = 10, scale: float = 2.0) -> str:
    """Render scanned PDF pages to bitmaps and OCR them."""
    out = []
    with objc.autorelease_pool():
        url = NSURL.fileURLWithPath_(path)
        doc = Quartz.CGPDFDocumentCreateWithURL(url)
        if doc is None:
            return ""
        pages = min(Quartz.CGPDFDocumentGetNumberOfPages(doc), max_pages)
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        for i in range(1, pages + 1):
            with objc.autorelease_pool():
                page = Quartz.CGPDFDocumentGetPage(doc, i)
                box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
                w = int(box.size.width * scale)
                h = int(box.size.height * scale)
                if w <= 0 or h <= 0:
                    continue
                ctx = Quartz.CGBitmapContextCreate(
                    None, w, h, 8, 0, cs, Quartz.kCGImageAlphaPremultipliedLast
                )
                Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
                Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, w, h))
                Quartz.CGContextScaleCTM(ctx, scale, scale)
                Quartz.CGContextDrawPDFPage(ctx, page)
                img = Quartz.CGBitmapContextCreateImage(ctx)
                handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
                text = _recognize(handler)
                if text:
                    out.append(text)
    return "\n\n".join(out)
