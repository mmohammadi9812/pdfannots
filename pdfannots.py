#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Extracts annotations from a PDF file in markdown format for use in reviewing.
"""

import argparse
import io
import sys
import os
import textwrap
from collections import defaultdict
from PyPDF2 import PdfFileReader

import pdfminer.pdftypes as pdftypes
import pdfminer.settings
import pdfminer.utils
from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams, LTContainer, LTAnno, LTChar, LTTextBox
from pdfminer.pdfdocument import PDFDocument, PDFNoOutlines
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.psparser import PSLiteralTable, PSLiteral

pdfminer.settings.STRICT = False

SUBSTITUTIONS = {
    u'ﬀ': 'ff',
    u'ﬁ': 'fi',
    u'ﬂ': 'fl',
    u'ﬃ': 'ffi',
    u'ﬄ': 'ffl',
    u'‘': "'",
    u'’': "'",
    u'“': '"',
    u'”': '"',
    u'…': '...',
}

ANNOT_SUBTYPES = frozenset(
    {'Text', 'Highlight', 'Squiggly', 'StrikeOut', 'Underline'})

COLUMNS_PER_PAGE = 2  # default only, changed via a command-line parameter

DEBUG_BOXHIT = False


def boxhit(item, box):
    (x0, y0, x1, y1) = box
    assert item.x0 <= item.x1 and item.y0 <= item.y1
    assert x0 <= x1 and y0 <= y1

    # does most of the item area overlap the box?
    # http://math.stackexchange.com/questions/99565/simplest-way-to-calculate-the-intersect-area-of-two-rectangles
    x_overlap = max(0, min(item.x1, x1) - max(item.x0, x0))
    y_overlap = max(0, min(item.y1, y1) - max(item.y0, y0))
    overlap_area = x_overlap * y_overlap
    item_area = (item.x1 - item.x0) * (item.y1 - item.y0)
    assert overlap_area <= item_area

    if DEBUG_BOXHIT and overlap_area != 0:
        print(
            "'%s' %f-%f,%f-%f in %f-%f,%f-%f %2.0f%%" %
            (item.get_text(),
             item.x0,
             item.x1,
             item.y0,
             item.y1,
             x0,
             x1,
             y0,
             y1,
             100 *
             overlap_area /
             item_area))

    if item_area == 0:
        return False
    else:
        return overlap_area >= 0.5 * item_area


class RectExtractor(TextConverter):
    def __init__(self, rsrcmgr, codec='utf-8', pageno=1, laparams=None):
        dummy = io.StringIO()
        TextConverter.__init__(
            self,
            rsrcmgr,
            outfp=dummy,
            codec=codec,
            pageno=pageno,
            laparams=laparams)
        self.annots = set()

    def setannots(self, annots):
        self.annots = {a for a in annots if a.boxes}

    # main callback from parent PDFConverter
    def receive_layout(self, ltpage):
        self._lasthit = frozenset()
        self._curline = set()
        self.render(ltpage)

    def testboxes(self, item):
        hits = frozenset({a for a in self.annots if any(
            {boxhit(item, b) for b in a.boxes})})
        self._lasthit = hits
        self._curline.update(hits)
        return hits

    # "broadcast" newlines to _all_ annotations that received any text on the
    # current line, in case they see more text on the next line, even if the
    # most recent character was not covered.
    def capture_newline(self):
        for a in self._curline:
            a.capture('\n')
        self._curline = set()

    def render(self, item):
        # If it's a container, recurse on nested items.
        if isinstance(item, LTContainer):
            for child in item:
                self.render(child)

            # Text boxes are a subclass of container, and somehow encode newlines
            # (this weird logic is derived from pdfminer.converter.TextConverter)
            if isinstance(item, LTTextBox):
                self.testboxes(item)
                self.capture_newline()

        # Each character is represented by one LTChar, and we must handle
        # individual characters (not higher-level objects like LTTextLine)
        # so that we can capture only those covered by the annotation boxes.
        elif isinstance(item, LTChar):
            for a in self.testboxes(item):
                a.capture(item.get_text())

        # Annotations capture whitespace not explicitly encoded in
        # the text. They don't have an (X,Y) position, so we need some
        # heuristics to match them to the nearby annotations.
        elif isinstance(item, LTAnno):
            text = item.get_text()
            if text == '\n':
                self.capture_newline()
            else:
                for a in self._lasthit:
                    a.capture(text)


class Page:
    def __init__(self, pageno, mediabox):
        self.pageno = pageno
        self.mediabox = mediabox
        self.annots = []

    def __eq__(self, other):
        return self.pageno == other.pageno

    def __lt__(self, other):
        return self.pageno < other.pageno


class Annotation:
    def __init__(
            self,
            page,
            tagname,
            coords=None,
            rect=None,
            contents=None,
            author=None):
        self.page = page
        self.tagname = tagname
        if contents == '':
            self.contents = None
        else:
            self.contents = contents
        self.rect = rect
        self.author = author
        self.text = ''

        if coords is None:
            self.boxes = None
        else:
            assert len(coords) % 8 == 0
            self.boxes = []
            while coords != []:
                (x0, y0, x1, y1, x2, y2, x3, y3) = coords[:8]
                coords = coords[8:]
                xvals = [x0, x1, x2, x3]
                yvals = [y0, y1, y2, y3]
                box = (min(xvals), min(yvals), max(xvals), max(yvals))
                self.boxes.append(box)

    def capture(self, text):
        if text == '\n':
            # Kludge for latex: elide hyphens
            if self.text.endswith('-'):
                self.text = self.text[:-1]

            # Join lines, treating newlines as space, while ignoring successive
            # newlines. This makes it easier for the for the renderer to
            # "broadcast" LTAnno newlines to active annotations regardless of
            # box hits. (Detecting paragraph breaks is tricky anyway, and left
            # for future future work!)
            elif not self.text.endswith(' '):
                self.text += ' '
        else:
            self.text += text

    def gettext(self):
        if self.boxes:
            if self.text:
                # replace tex ligatures (and other common odd characters)
                return ''.join([SUBSTITUTIONS.get(c, c)
                                for c in self.text.strip()])
            else:
                # something's strange -- we have boxes but no text for them
                return "(XXX: missing text!)"
        else:
            return None

    def getstartpos(self):
        if self.rect:
            (x0, y0, x1, y1) = self.rect
        elif self.boxes:
            (x0, y0, x1, y1) = self.boxes[0]
        else:
            return None
        # XXX: assume left-to-right top-to-bottom text
        return Pos(self.page, min(x0, x1), max(y0, y1))

    # custom < operator for sorting
    def __lt__(self, other):
        return self.getstartpos() < other.getstartpos()


class Pos:
    def __init__(self, page, x, y):
        self.page = page
        self.x = x
        self.y = y

    def __lt__(self, other):
        if self.page < other.page:
            return True
        elif self.page == other.page:
            assert self.page is other.page
            # XXX: assume left-to-right top-to-bottom documents
            (sx, sy) = self.normalise_to_mediabox()
            (ox, oy) = other.normalise_to_mediabox()
            (x0, y0, x1, y1) = self.page.mediabox
            colwidth = (x1 - x0) / COLUMNS_PER_PAGE
            self_col = (sx - x0) // colwidth
            other_col = (ox - x0) // colwidth
            return self_col < other_col or (self_col == other_col and sy > oy)
        else:
            return False

    def normalise_to_mediabox(self):
        x, y = self.x, self.y
        (x0, y0, x1, y1) = self.page.mediabox
        if x < x0:
            x = x0
        elif x > x1:
            x = x1
        if y < y0:
            y = y0
        elif y > y1:
            y = y1
        return (x, y)


def getannots(pdfannots, page):
    annots = []
    for pa in pdfannots:
        subtype = pa.get('Subtype')
        if subtype is not None and subtype.name not in ANNOT_SUBTYPES:
            continue

        contents = pa.get('Contents')
        if contents is not None:
            # decode as string, normalise line endings, replace special
            # characters
            contents = pdfminer.utils.decode_text(contents)
            contents = contents.replace('\r\n', '\n').replace('\r', '\n')
            contents = ''.join([SUBSTITUTIONS.get(c, c) for c in contents])

        coords = pdftypes.resolve1(pa.get('QuadPoints'))
        rect = pdftypes.resolve1(pa.get('Rect'))
        author = pdftypes.resolve1(pa.get('T'))
        if author is not None:
            author = pdfminer.utils.decode_text(author)
        a = Annotation(
            page,
            subtype.name,
            coords,
            rect,
            contents,
            author=author)
        annots.append(a)

    return annots


class OrgPrinter:
    """
    OrgPrinter is used to extract annotations in org-mode format
    """

    def __init__(self, outlines, wrapcol, outfile):
        """
        outlines List of outlines
        wrapcol  If not None, specifies the column at which output is word-wrapped
        """
        self.outlines = outlines
        self.wrapcol = wrapcol

        self.outfile = outfile

        self.annot_nits = frozenset({'Squiggly', 'StrikeOut', 'Underline'})

        self.INDENT = " "

        if wrapcol:
            self.text_wrapper = textwrap.TextWrapper(
                width=wrapcol,
                initial_indent=self.INDENT * 2,
                subsequent_indent=self.INDENT * 2
            )

            self.indent_wrapper = textwrap.TextWrapper(
                width=wrapcol,
                initial_indent=self.INDENT,
                subsequent_indent=self.INDENT
            )

    def nearest_outline(self, pos):
        prev = None
        for o in self.outlines:
            if o.pos < pos:
                prev = o
            else:
                break
        return prev

    def format_pos(self, annot):
        apos = annot.getstartpos()
        o = self.nearest_outline(apos) if apos else None
        return f"** {annot.page.pageno + 1}" + (f" {o.title}" if o else "")

    def format_bullet(self, paras, quotepos=None, quotelen=None):
        if quotepos:
            assert quotepos > 0 and quotelen > 0 and quotepos + \
                quotelen <= len(paras)

        # emit the first paragraph with the bullet
        # if self.wrapcol:
        #     ret = self.text_wrapper.fill(paras[0])
        # else:
        #     ret = self.INDENT + paras[0]
        ret = ""
        page_number = paras[0]

        # emit subsequent paragraphs
        npara = 1
        for para in paras[1:]:
            # are we in a blockquote?
            inquote = quotepos and npara >= quotepos and npara < quotepos + quotelen

            # emit a paragraph break
            # if we're going straight to a quote, we don't need an extra
            # newline
            ret = ret + ('\n' if npara == quotepos else '\n\n')

            if self.wrapcol:
                tw = self.text_wrapper if inquote else self.indent_wrapper
                ret = ret + tw.fill(para)
            else:
                # indent = self.HEADER_INDENT * 2 + self.INDENT if inquote else self.INDENT
                indent = self.INDENT * 2
                ret = ret + indent + para

            npara += 1

        return page_number, ret

    def format_annot(self, annot, extra=None):
        # capture item text and contents (i.e. the comment), and split each
        # into paragraphs
        rawtext = annot.gettext()
        text = [l for l in rawtext.strip().splitlines()
                if l] if rawtext else []
        comment = [l for l in annot.contents.splitlines()
                   if l] if annot.contents else []

        # we are either printing: item text and item contents, or one of the two
        # if we see an annotation with neither, something has gone wrong
        assert text or comment

        # compute the formatted position (and extra bit if needed) as a label
        label = self.format_pos(
            annot) + (f":PROPERTIES:\n:Extra: {extra}\n:END:\n" if extra else "")

        # If we have short (single-paragraph, few words) text with a short or no
        # comment, and the text contains no embedded full stops or quotes, then
        # we'll just put quotation marks around the text and merge the two into
        # a single paragraph.
        # if (text and len(text) == 1 and len(text[0].split()) <= 10  # words
        #         and all([x not in text[0] for x in ['"', '. ']])
        #         and (not comment or len(comment) == 1)):
        #     msg = label + ' "' + text[0] + '"'
        #     if comment:
        #         msg = msg + ' -- ' + comment[0]
        #     return self.format_bullet([msg]) + "\n"

        # If there is no text and a single-paragraph comment, it also goes on
        # one line.
        # if comment and not text and len(comment) == 1:
        #     msg = label + " " + comment[0]
        #     return self.format_bullet([msg]) + "\n"

        # Otherwise, text (if any) turns into a blockquote, and the comment (if
        # any) into subsequent paragraphs.
        # else:
        msgparas = [label] + text + comment
        quotepos = 1 if text else None
        quotelen = len(text) if text else None
        return self.format_bullet(msgparas, quotepos, quotelen)

    def printall(self, annots):
        for a in annots:
            print(self.format_annot(a, a.tagname), file=self.outfile)

    def printall_grouped(self, sections, annots):
        """
        sections controls the order of sections output
                e.g.: ["highlights", "comments", "nits"]
        """
        self._printheader_called = False

        def printheader(name):
            # emit blank separator line if needed
            if self._printheader_called:
                print("", file=self.outfile)
            else:
                self._printheader_called = True
            print(f"* {name}\n", file=self.outfile)

        highlights = [a for a in annots if a.tagname ==
                      'Highlight' and a.contents is None]
        comments = [
            a for a in annots if a.tagname not in self.annot_nits and a.contents]
        nits = [a for a in annots if a.tagname in self.annot_nits]

        page_highlights, page_comments, page_nits = defaultdict(
            str), defaultdict(str), defaultdict(str)

        for section_name in sections:
            if highlights and section_name == 'highlights':
                printheader("Highlights")
                for a in highlights:
                    ph = self.format_annot(a)
                    page_highlights[ph[0]] += ph[1] + '\n'
                for k, v in page_highlights.items():
                    print(k + v, file=self.outfile)

            if comments and section_name == 'comments':
                printheader("Detailed comments")
                for a in comments:
                    ps = self.format_annot(a)
                    page_comments[ps[0]] = ps[1]
                for k, v in page_comments.items():
                    print(k + v, file=self.outfile)

            if nits and section_name == 'nits':
                printheader("Nits")
                for a in nits:
                    if a.tagname == 'StrikeOut':
                        extra = "delete"
                    else:
                        extra = None
                    pn = self.format_annot(a, extra)
                    page_nits[pn[0]] = pn[1]
                for k, v in page_nits.items():
                    print(k + v, file=self.outfile)


def resolve_dest(doc, dest):
    if isinstance(dest, bytes):
        dest = pdftypes.resolve1(doc.get_dest(dest))
    elif isinstance(dest, PSLiteral):
        dest = pdftypes.resolve1(doc.get_dest(dest.name))
    if isinstance(dest, dict):
        dest = dest['D']
    return dest


class Outline:
    def __init__(self, title, dest, pos):
        self.title = title
        self.dest = dest
        self.pos = pos


def get_outlines(doc, pageslist, pagesdict):
    result = []
    for (_, title, destname, actionref, _) in doc.get_outlines():
        if destname is None and actionref:
            action = pdftypes.resolve1(actionref)
            if isinstance(action, dict):
                subtype = action.get('S')
                if subtype is PSLiteralTable.intern('GoTo'):
                    destname = action.get('D')
        if destname is None:
            continue
        dest = resolve_dest(doc, destname)

        # consider targets of the form [page /XYZ left top zoom]
        if dest[1] is PSLiteralTable.intern('XYZ'):
            (pageref, _, targetx, targety) = dest[:4]

            if isinstance(pageref, int):
                page = pageslist[pageref]
            elif isinstance(pageref, pdftypes.PDFObjRef):
                page = pagesdict[pageref.objid]
            else:
                sys.stderr.write(
                    'Warning: unsupported pageref in outline: %s\n' %
                    pageref)
                page = None

            if page:
                pos = Pos(page, targetx, targety)
                result.append(Outline(title, destname, pos))
    return result


def pdftitle(fh):
    pdf_reader = PdfFileReader(fh)
    docinfo = pdf_reader.getDocumentInfo()
    return docinfo.title if (docinfo and docinfo.title) else ''


def process_file(fh, emit_progress):
    rsrcmgr = PDFResourceManager()
    laparams = LAParams()
    device = RectExtractor(rsrcmgr, laparams=laparams)
    interpreter = PDFPageInterpreter(rsrcmgr, device)
    parser = PDFParser(fh)
    doc = PDFDocument(parser)

    pageslist = []  # pages in page order
    pagesdict = {}  # map from PDF page object ID to Page object
    allannots = []

    for (pageno, pdfpage) in enumerate(PDFPage.create_pages(doc)):
        page = Page(pageno, pdfpage.mediabox)
        pageslist.append(page)
        pagesdict[pdfpage.pageid] = page
        if pdfpage.annots:
            # emit progress indicator
            if emit_progress:
                sys.stderr.write(
                    (" " if pageno > 0 else "") + "%d" %
                    (pageno + 1))
                sys.stderr.flush()

            pdfannots = []
            for a in pdftypes.resolve1(pdfpage.annots):
                if isinstance(a, pdftypes.PDFObjRef):
                    pdfannots.append(a.resolve())
                else:
                    sys.stderr.write('Warning: unknown annotation: %s\n' % a)

            page.annots = getannots(pdfannots, page)
            page.annots.sort()
            device.setannots(page.annots)
            interpreter.process_page(pdfpage)
            allannots.extend(page.annots)

    if emit_progress:
        sys.stderr.write("\n")

    outlines = []
    try:
        outlines = get_outlines(doc, pageslist, pagesdict)
    except PDFNoOutlines:
        if emit_progress:
            sys.stderr.write(
                "Document doesn't include outlines (\"bookmarks\")\n")
    except Exception as ex:
        sys.stderr.write("Warning: failed to retrieve outlines: %s\n" % ex)

    device.close()

    return allannots, outlines


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)

    p.add_argument("input", metavar="INFILE", type=argparse.FileType("rb"),
                   help="PDF files to process", nargs='+')

    g = p.add_argument_group('Basic options')
    g.add_argument("-p", "--progress", default=False, action="store_true",
                   help="emit progress information")
    g.add_argument(
        "-n",
        "--cols",
        default=2,
        type=int,
        metavar="COLS",
        dest="cols",
        help="number of columns per page in the document (default: 2)")

    g = p.add_argument_group('Options controlling output format')
    allsects = ["highlights", "comments", "nits"]
    g.add_argument(
        "-s",
        "--sections",
        metavar="SEC",
        nargs="*",
        choices=allsects,
        default=allsects,
        help=(
                "sections to emit (default: %s)" %
                ', '.join(allsects)))
    g.add_argument(
        "--no-group",
        dest="group",
        default=True,
        action="store_false",
        help="emit annotations in order, don't group into sections")
    g.add_argument(
        "--print-filename",
        dest="printfilename",
        default=False,
        action="store_true",
        help="print the filename when it has annotations")
    g.add_argument("-w", "--wrap", metavar="COLS", type=int,
                   help="wrap text at this many output columns")

    return p.parse_args()


def main():
    args = parse_args()
    global COLUMNS_PER_PAGE
    COLUMNS_PER_PAGE = args.cols
    for file in args.input:
        (annots, outlines) = process_file(file, args.progress)
        orgfilename = os.path.splitext(os.path.basename(file.name))[0]
        orgfile = open(orgfilename + '.org', 'w')
        op = OrgPrinter(outlines, args.wrap, orgfile)
        title = pdftitle(file)
        if args.printfilename and annots:
            print(f"#+Title: {title if title else file.name}\n", file=orgfile)
        if args.group:
            op.printall_grouped(args.sections, annots)
        else:
            op.printall(annots)
        orgfile.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
