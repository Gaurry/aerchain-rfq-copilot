"""Voice input: the browser's own speech recognition (Chrome / Edge), running on
the app page. The buyer clicks, speaks, pauses; the final words come back to
Python once, as a trigger value. No server-side audio, no extra API key."""
from __future__ import annotations

import streamlit as st

HTML = """
<div class="vrow">
  <button class="vbtn" type="button">🎙️ Speak</button>
  <div class="vlive">Click, speak, then pause. It sends when you stop.</div>
</div>
"""

CSS = """
.vrow { display: flex; gap: 8px; align-items: center; margin: 4px 0 8px; }
.vbtn { flex: 0 0 auto; border: 1px solid var(--st-border-color, rgba(49,51,63,.25));
        background: var(--st-secondary-background-color, #f0f2f6); color: var(--st-text-color, inherit);
        border-radius: 8px; padding: 8px 14px; font-size: 14px; cursor: pointer; }
.vbtn.on { background: #ff4b4b; color: #fff; border-color: #ff4b4b; }
.vbtn:disabled { opacity: .45; cursor: not-allowed; }
.vlive { font-size: 13px; opacity: .8; line-height: 1.3; }
"""

JS = """
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const btn = parentElement.querySelector('.vbtn');
  const live = parentElement.querySelector('.vlive');
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const opts = data || {};
  let rec = null, listening = false, finalText = '';

  if (!SR) { btn.disabled = true; live.textContent = 'Voice needs Chrome or Edge. You can type instead.'; return; }
  btn.disabled = !!opts.disabled;
  if (opts.disabled && opts.reason) live.textContent = opts.reason;

  const stopUi = () => { listening = false; btn.classList.remove('on'); btn.textContent = '🎙️ Speak'; };

  btn.onclick = () => {
    if (listening && rec) { rec.stop(); return; }
    rec = new SR();
    rec.lang = opts.lang || 'en-IN';
    rec.interimResults = true; rec.continuous = false; rec.maxAlternatives = 1;
    finalText = '';
    rec.onstart = () => { listening = true; btn.classList.add('on'); btn.textContent = '■ Stop'; live.textContent = 'Listening…'; };
    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += t; else interim += t;
      }
      live.textContent = (finalText + ' ' + interim).trim() || 'Listening…';
    };
    rec.onerror = (e) => {
      const why = {'not-allowed': 'Microphone blocked. Allow it from the lock icon in the address bar.',
                   'service-not-allowed': 'Microphone blocked. Allow it from the lock icon in the address bar.',
                   'no-speech': "Didn't hear anything. Try again.",
                   'audio-capture': 'No microphone found.',
                   'network': 'Speech service unreachable. Type instead.'}[e.error] || ('Voice error: ' + e.error);
      live.textContent = why;
    };
    rec.onend = () => {
      stopUi();
      const text = finalText.trim();
      if (text) { live.textContent = 'Sent: “' + text + '”'; setTriggerValue('said', text); }
    };
    try { rec.start(); } catch (err) { live.textContent = "Couldn't start the mic: " + err.message; stopUi(); }
  };

  return () => { try { if (rec) rec.abort(); } catch (e) {} };
}
"""

_component = st.components.v2.component("rfq_voice_input", html=HTML, css=CSS, js=JS)


def voice_input(disabled: bool = False, reason: str | None = None, key: str = "voice") -> str | None:
    """Returns the spoken words once, on the run right after the buyer stops speaking."""
    result = _component(data={"lang": "en-IN", "disabled": disabled, "reason": reason},
                        key=key, on_said_change=lambda: None)
    return getattr(result, "said", None)
