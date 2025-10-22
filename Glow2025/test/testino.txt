#include <Arduino.h>

String inputBuffer = "";
bool messageStarted = false;
const int LED_PIN = LED_BUILTIN;

struct DelayedCommand {
  String source;            // e.g., "ARM1", "CENTER", "TOP"
  String command;           // STAR_ARRIVED, CLIMAX_READY, CLIMAX_DONE_CENTER, CLIMAX_DONE_TOP
  unsigned long triggerTime;
  bool active;
};

const int MAX_DELAYED = 8;   // a few concurrent delayed events
DelayedCommand delayed[MAX_DELAYED];

// --- Track first-time drops (tiny memory footprint)
bool droppedMake         = false;
bool droppedUpdate       = false;
bool droppedSend         = false;
bool droppedAddTop       = false;
bool droppedAddCenter    = false;
bool droppedBuildup      = false;  // BUILDUP_CLIMAX_CENTER
bool droppedStartCenter  = false;  // START_CLIMAX_CENTER
bool droppedStartTop     = false;  // START_CLIMAX_TOP

// Utility: schedule a delayed REQUEST from a device
// default delay 5–8s, suitable for animation/buildup simulation
void scheduleDelayedCommand(const String& source, const String& command, unsigned long minDelay = 5000UL, unsigned long maxDelay = 8000UL) {
  for (int i = 0; i < MAX_DELAYED; i++) {
    if (!delayed[i].active) {
      delayed[i].source = source;
      delayed[i].command = command;
      unsigned long span = (maxDelay > minDelay) ? (maxDelay - minDelay) : 0;
      unsigned long jitter = span ? (random(span)) : 0;
      delayed[i].triggerTime = millis() + minDelay + jitter;
      delayed[i].active = true;
      return;
    }
  }
  // no space; silently drop to keep sketch tiny
}

void setup() {
  delay(2000);
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  randomSeed(analogRead(0));
  for (int i = 0; i < MAX_DELAYED; i++) delayed[i].active = false;
}

void loop() {
  // --- Assemble frames "!!...##" ---
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '!' && !messageStarted) {
      messageStarted = true;
      inputBuffer = "!!";
    } else if (messageStarted) {
      inputBuffer += c;
      if (inputBuffer.endsWith("##")) {
        handleMessage(inputBuffer);
        inputBuffer = "";
        messageStarted = false;
      }
      if (inputBuffer.length() > 200) { // simple overflow guard
        inputBuffer = "";
        messageStarted = false;
      }
    }
  }

  // --- Emit delayed REQUESTs ---
  unsigned long now = millis();
  for (int i = 0; i < MAX_DELAYED; i++) {
    if (delayed[i].active && now >= delayed[i].triggerTime) {
      sendRequest(delayed[i].source, delayed[i].command);
      delayed[i].active = false;
    }
  }
}

// ----------------------------------------------------
// Core frame handler (tiny parser: "!!<src>:<type>:<cmd>{...}##")
// We only care about the src and cmd (type is always REQUEST from Mac).
// ----------------------------------------------------
void handleMessage(String msg) {
  msg.trim();
  if (!msg.startsWith("!!") || !msg.endsWith("##")) return;

  // strip !! and ##
  msg.remove(0, 2);
  msg.remove(msg.length() - 2, 2);

  int c1 = msg.indexOf(':');
  int c2 = msg.indexOf(':', c1 + 1);
  if (c1 < 0 || c2 < 0) return;

  String source = msg.substring(0, c1);       // e.g., "ARM1", "CENTER", "TOP"
  String command = msg.substring(c2 + 1);     // e.g., "MAKE_STAR{50}" or "SEND_STAR"
  int bracePos = command.indexOf('{');
  if (bracePos >= 0) command = command.substring(0, bracePos);
  command.trim();

  // ---- One-time drop to force Mac retries ----
  if (command.equalsIgnoreCase("MAKE_STAR") && !droppedMake) {
    droppedMake = true;      // drop confirm once
    return;
  }
  if (command.equalsIgnoreCase("UPDATE_STAR") && !droppedUpdate) {
    droppedUpdate = true;
    return;
  }
  if (command.equalsIgnoreCase("SEND_STAR") && !droppedSend) {
    droppedSend = true;
    return;
  }
  if (command.equalsIgnoreCase("ADD_STAR_TOP") && !droppedAddTop) {
    droppedAddTop = true;
    return;
  }
  if (command.equalsIgnoreCase("ADD_STAR_CENTER") && !droppedAddCenter) {
    droppedAddCenter = true;
    return;
  }
  if (command.equalsIgnoreCase("BUILDUP_CLIMAX_CENTER") && !droppedBuildup) {
    droppedBuildup = true;
    return;
  }
  if (command.equalsIgnoreCase("START_CLIMAX_CENTER") && !droppedStartCenter) {
    droppedStartCenter = true;
    return;
  }
  if (command.equalsIgnoreCase("START_CLIMAX_TOP") && !droppedStartTop) {
    droppedStartTop = true;
    return;
  }

  // ---- Normal path: send CONFIRM, and schedule any follow-up REQUESTs ----
  sendConfirm(source, command);

  // Star animation result later
  if (command.equalsIgnoreCase("SEND_STAR")) {
    scheduleDelayedCommand(source, "STAR_ARRIVED", 4000UL, 7000UL); // 4–7s animation
  }

  // Climax buildup: CENTER will later say CLIMAX_READY
  if (command.equalsIgnoreCase("BUILDUP_CLIMAX_CENTER")) {
    scheduleDelayedCommand(source, "CLIMAX_READY", 3000UL, 5000UL); // 3–5s buildup
  }

  // After STARTs: later send DONEs from respective devices
  if (command.equalsIgnoreCase("START_CLIMAX_CENTER")) {
    scheduleDelayedCommand(source, "CLIMAX_DONE_CENTER", 5000UL, 8000UL); // 5–8s
  }
  if (command.equalsIgnoreCase("START_CLIMAX_TOP")) {
    scheduleDelayedCommand(source, "CLIMAX_DONE_TOP", 5000UL, 8000UL);    // 5–8s
  }
}

// ----------------------------------------------------
// Send a CONFIRM for a REQUEST we just received
// ----------------------------------------------------
void sendConfirm(String source, const String& command) {
  // normalize "!!" prefixed accidental sources if any
  while (source.startsWith("!")) source.remove(0, 1);

  // Confirm frame: !!<source>:MASTER:CONFIRM:<command>##
  String msg = "!!" + source + ":MASTER:CONFIRM:" + command + "##";
  Serial.println(msg);
}

// ----------------------------------------------------
// Send a REQUEST event from a device to MASTER (delayed)
// ----------------------------------------------------
void sendRequest(const String& source, const String& command) {
  String s = source;
  while (s.startsWith("!")) s.remove(0, 1);

  // Map delayed command to proper REQUEST payloads
  if (command.equalsIgnoreCase("STAR_ARRIVED")) {
    String msg = "!!" + s + ":MASTER:REQUEST:STAR_ARRIVED{";
    msg += "SPEED=" + String(random(10, 100)) + ",";
    msg += "COLOR=" + String(random(0, 255)) + ",";
    msg += "BRIGHTNESS=" + String(random(50, 255)) + ",";
    msg += "SIZE=" + String(random(1, 10)) + "}##";
    Serial.println(msg);
    return;
  }

  if (command.equalsIgnoreCase("CLIMAX_READY")) {
    String msg = "!!" + s + ":MASTER:REQUEST:CLIMAX_READY##";
    Serial.println(msg);
    return;
  }

  if (command.equalsIgnoreCase("CLIMAX_DONE_CENTER")) {
    String msg = "!!" + s + ":MASTER:REQUEST:CLIMAX_DONE_CENTER##";
    Serial.println(msg);
    return;
  }

  if (command.equalsIgnoreCase("CLIMAX_DONE_TOP")) {
    String msg = "!!" + s + ":MASTER:REQUEST:CLIMAX_DONE_TOP##";
    Serial.println(msg);
    return;
  }

  // Fallback: if we ever schedule a non-REQUEST token accidentally, just CONFIRM it
  sendConfirm(s, command);
}
