"""An annotated MJPEG stream, for seeing what the camera sees.

Deliberately capped well below the camera's framerate: this exists to be looked
at by a human over a network, not to feed a pipeline.

Requires the camera to be free. While PhotonVision is running it owns the
device, and PhotonVision publishes its own streams on 1181/1182 - use those
instead of stopping it.
"""

import socket
import time

from . import camera as cam


def serve(dev, port=8090, family="DICT_APRILTAG_36h11", fps_cap=15):
    import cv2
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    det = None
    try:
        d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, family))
        det = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())
    except Exception:
        pass

    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    if not cap.isOpened():
        who = cam.holder(dev)
        print("could not open %s%s" % (dev, " - held by pid %d (%s)" % who
                                       if isinstance(who, tuple) else ""))
        return 1
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 800)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            last = time.perf_counter()
            while True:
                ok, frame = cap.read()
                if not ok:
                    continue
                now = time.perf_counter()
                rate, last = 1.0 / max(now - last, 1e-6), now
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                note = "mean %.0f   %.0f fps" % (gray.mean(), rate)
                if det is not None:
                    corners, ids, _ = det.detectMarkers(gray)
                    if ids is not None:
                        cv2.aruco.drawDetectedMarkers(frame, corners, ids)
                        p = corners[0].reshape(4, 2)
                        note = "id %d  %.0f px wide   %s" % (
                            ids.flatten()[0], max(p[:, 0]) - min(p[:, 0]), note)
                    else:
                        note = "no tag   " + note
                cv2.putText(frame, note, (16, 40), cv2.FONT_HERSHEY_SIMPLEX,
                            0.9, (0, 255, 0), 2)
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not ok:
                    continue
                try:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                                     + jpg.tobytes() + b"\r\n")
                except Exception:
                    return                       # client went away
                time.sleep(1.0 / fps_cap)

    host = socket.gethostname()
    ip, _ = cam.addresses()
    print("streaming  (ctrl-C to stop)")
    if ip:
        print("  http://%s:%d" % (ip, port))
    print("  http://%s:%d" % (host if host.endswith(".local") else host + ".local", port))
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
    return 0
