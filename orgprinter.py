import textwrap
from  collections import defaultdict


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

        page_highlights, page_comments, page_nits = defaultdict(str), defaultdict(str), defaultdict(str)

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
