from PyPDF2 import PdfFileReader


def pdftitle(fh):
    pdf_reader = PdfFileReader(fh)
    docinfo = pdf_reader.getDocumentInfo()
    return docinfo.title if (docinfo and docinfo.title) else ''
