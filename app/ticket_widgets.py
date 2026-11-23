"""Ticket capture worker; browsing and generation live in separate widgets."""
import json,sqlite3,threading,time
from pathlib import Path
from PySide6.QtCore import QThread,Signal
from ticket_capture import TicketCapture,DEFAULT_INPUT,DEFAULT_OUTPUT

def settings(root):
    try:
        data=json.loads((Path(root)/'ticket_settings.json').read_text(encoding='utf-8-sig'))
        return Path(data.get('input_dir',DEFAULT_INPUT)),Path(data.get('output_dir',DEFAULT_OUTPUT))
    except (OSError,ValueError,TypeError):return DEFAULT_INPUT,DEFAULT_OUTPUT

class TicketWorker(QThread):
    saved=Signal(dict);status=Signal(dict)
    def __init__(self,root):
        super().__init__();self.root=Path(root);self.stop_event=threading.Event()
    def run(self):
        capture=None;current=None;last_status=0
        try:
            while not self.stop_event.is_set():
                desired=settings(self.root)
                if current!=desired:
                    if capture:capture.close();capture=None
                    try:capture=TicketCapture(*desired);current=desired
                    except (OSError,ValueError,sqlite3.Error) as error:
                        self.status.emit({'running':False,'input_dir':str(desired[0]),'output_dir':str(desired[1]),'error':str(error)})
                        self.stop_event.wait(2);continue
                for entry in capture.poll(stop_requested=self.stop_event.is_set):self.saved.emit(entry)
                if time.monotonic()-last_status>1:
                    self.status.emit({'running':capture.input_dir.is_dir(),'input_dir':str(current[0]),'output_dir':str(current[1]),'saved':capture.saved_count,'pending':len(capture.pending),'error':capture.last_error})
                    last_status=time.monotonic()
                self.stop_event.wait(.25)
        except Exception as error:self.status.emit({'running':False,'error':str(error)})
        finally:
            if capture:capture.close()
    def stop(self):self.stop_event.set();return self.wait(10000)
