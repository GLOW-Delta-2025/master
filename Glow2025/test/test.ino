#include <Arduino.h>

String inputBuffer = "";
bool messageStarted = false;
const int LED_PIN = LED_BUILTIN;

struct DelayedCommand {
  String source;
  String command;
  unsigned long triggerTime;
  bool active;
};

const int MAX_DELAYED = 5;
DelayedCommand delayed[MAX_DELAYED];

// --- Track first-time drops (tiny memory footprint)
bool droppedMake = false;
bool droppedUpdate = false;
bool droppedSend = false;
bool droppedAddTop = false;
bool droppedAddCenter = false;

void scheduleDelayedCommand(String source, String command) {
  for (int i = 0; i < MAX_DELAYED; i++) {
    if (!delayed[i].active) {
      delayed[i].source = source;
      delayed[i].command = command;
      delayed[i].triggerTime = millis() + random(5000, 8000);
      delayed[i].active = true;
      return;
    }
  }
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
      if (inputBuffer.length() > 200) {
        inputBuffer = "";
        messageStarted = false;
      }
    }
  }

  unsigned long now = millis();
  for (int i = 0; i < MAX_DELAYED; i++) {
    if (delayed[i].active && now >= delayed[i].triggerTime) {
      sendConfirm(delayed[i].source, delayed[i].command);
      delayed[i].active = false;
    }
  }
}

void handleMessage(String msg) {
  msg.trim();
  if (!msg.startsWith("!!") || !msg.endsWith("##")) return;
  msg.remove(0, 2);
  msg.remove(msg.length() - 2, 2);

  int c1 = msg.indexOf(':');
  int c2 = msg.indexOf(':', c1 + 1);
  if (c1 < 0 || c2 < 0) return;

  String source = msg.substring(0, c1);
  String command = msg.substring(c2 + 1);
  int bracePos = command.indexOf('{');
  if (bracePos >= 0) command = command.substring(0, bracePos);
  command.trim();

  // --- Fail once per command ---
  if (command.equalsIgnoreCase("MAKE_STAR") && !droppedMake) {
    droppedMake = true;
    return; // drop first MAKE_STAR
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

  // --- Now respond normally ---
  if (command.equalsIgnoreCase("SEND_STAR")) {
    scheduleDelayedCommand(source, "STAR_ARRIVED");
  }

  sendConfirm(source, command);
}

void sendConfirm(String source, String command) {
    while (source.startsWith("!")) {
    source.remove(0, 1);
  }
  String msg = "!!" + source + ":MASTER:CONFIRM:" + command + "##";

  if (command.equalsIgnoreCase("STAR_ARRIVED")) {
    msg = "!!" + source + ":MASTER:REQUEST:STAR_ARRIVED{";
    msg += "SPEED=" + String(random(10, 100)) + ",";
    msg += "COLOR=" + String(random(0, 255)) + ",";
    msg += "BRIGHTNESS=" + String(random(50, 255)) + ",";
    msg += "SIZE=" + String(random(1, 10)) + "}##";
  }

  Serial.println(msg);
}
