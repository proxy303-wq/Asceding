"""Extract text from the ML research PDFs in Downloads."""
from pathlib import Path

from pypdf import PdfReader

FILES = [
    (r"C:\Users\tgowd\Downloads\analysisandpredictionofindianstockmarket-amachinelearningapproach3.pdf",
     r"C:\Users\tgowd\Downloads\dhan-auto-trader\analysis\paper_ml_india.txt"),
    (r"C:\Users\tgowd\Downloads\Applying_machine_learning_method_in_stock_trading_.pdf",
     r"C:\Users\tgowd\Downloads\dhan-auto-trader\analysis\paper_ml_stocktrading.txt"),
]

for src, out in FILES:
    try:
        rd = PdfReader(src)
        txt = "\n".join((p.extract_text() or "") for p in rd.pages)
        Path(out).write_text(txt, encoding="utf-8")
        print(out, len(txt), "chars,", len(rd.pages), "pages")
    except Exception as e:
        print("ERR", src, e)
