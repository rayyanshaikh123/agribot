import { APK_PATH } from '@/lib/content';

export function Download() {
  return (
    <section id="download" className="download" aria-label="Download">
      <div className="download-inner">
        <h2>Put AgriBot to work.</h2>
        <p>Install the app, connect the robot over Bluetooth and share your network. Your first scan takes a minute.</p>
        <a
          className="apk-btn"
          href={APK_PATH}
          target="_blank"
          rel="noreferrer"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.6 9.48l1.84-3.18a.38.38 0 00-.66-.38l-1.86 3.22a11.5 11.5 0 00-9.84 0L5.22 5.92a.38.38 0 10-.66.38L6.4 9.48A10.78 10.78 0 001 18h22a10.78 10.78 0 00-5.4-8.52zM7 15.25a1.25 1.25 0 110-2.5 1.25 1.25 0 010 2.5zm10 0a1.25 1.25 0 110-2.5 1.25 1.25 0 010 2.5z" /></svg>
          <span><small>Download for</small>Android (APK)</span>
        </a>
        <p className="apk-note">Android 6.0 or newer · iPhone version coming soon</p>
      </div>
      <footer className="footer">
        <span>© {new Date().getFullYear()} AgriBot</span>
        <span>Smart farming, simplified.</span>
      </footer>
    </section>
  );
}
