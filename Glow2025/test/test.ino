/*
  Glow 25' Teensy Simulator - 5 Arms
  Simulates ARM1–ARM5 with proper star creation, updates, and arrival
  Confirms commands, sends STAR_ARRIVED, and supports idle/max brightness timers
*/

#define SERIAL_BAUD 115200
#define MAX_BRIGHTNESS 255
#define SEND_DELAY_MS 10000       // 10 seconds max timer
#define IDLE_TIMEOUT_MS 2000      // 2 seconds since last peak

#include <Arduino.h>

String inputBuffer = "";

struct ArmState {
  bool active = false;
  int brightness = 0;
  unsigned long startTime = 0;      // when MAKE_STAR started
  unsigned long lastPeakTime = 0;   // last UPDATE_STAR time
};

ArmState arms[5];

void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial); // wait for serial connection
  Serial.println("Glow25 Teensy Simulator Ready");

  // initialize all arms
  for (int i = 0; i < 5; i++) {
    arms[i] = ArmState();
  }
}

void loop() {
  readSerialMessages();
  checkArmsTimers();
}

void readSerialMessages() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    inputBuffer += c;

    // detect full message (!! ... ##)
    if (inputBuffer.endsWith("##")) {
      String msg = inputBuffer;
      inputBuffer = "";
      handleMessage(msg);
    }
  }
}

void handleMessage(String msg) {
  msg.trim();
  if (!msg.startsWith("!!")) return;

  int start = msg.indexOf('[');
  int end = msg.indexOf(']');
  if (start == -1 || end == -1) return;

  String target = msg.substring(start + 1, end);
  String request = extractRequestType(msg);
  int armIndex = getArmIndex(target);
  if (request == "" || armIndex < 0) return;

  // Send confirm once
  sendConfirm(target, request);

  // Handle commands
  if (request == "MAKE_STAR") {
    if (!arms[armIndex].active) {
      arms[armIndex].active = true;
      arms[armIndex].brightness = 0;
      arms[armIndex].startTime = millis();
      arms[armIndex].lastPeakTime = millis();
    }
  }
  else if (request == "UPDATE_STAR") {
    if (arms[armIndex].active) {
      int newBrightness = extractBrightness(msg);
      arms[armIndex].brightness = min(newBrightness, MAX_BRIGHTNESS);
      arms[armIndex].lastPeakTime = millis();
    }
  }
  else if (request == "SEND_STAR") {
    // manually trigger star send
    if (arms[armIndex].active) {
      sendStarArrived(target);
      arms[armIndex].active = false;
      arms[armIndex].brightness = 0;
    }
  }
}

String extractRequestType(String msg) {
  int secondColon = msg.indexOf(':', msg.indexOf(':') + 1);
  if (secondColon == -1) return "";

  int endPos = msg.indexOf('##', secondColon);
  if (endPos == -1) endPos = msg.length();

  String part = msg.substring(secondColon + 1, endPos);
  part.replace("##", "");
  part.trim();

  int brace = part.indexOf('{');
  if (brace != -1) part = part.substring(0, brace);

  int bracket = part.indexOf('[');
  if (bracket != -1) part = part.substring(0, bracket);

  part.trim();
  return part;
}

int extractBrightness(String msg) {
  int first = msg.indexOf('[');
  int last = msg.lastIndexOf(']');
  if (first == -1 || last == -1) return 0;

  String params = msg.substring(first, last + 1);
  int count = 0;
  int start = 0;
  while (true) {
    int open = params.indexOf('[', start);
    if (open == -1) break;
    int close = params.indexOf(']', open);
    if (close == -1) break;

    count++;
    if (count == 3) return params.substring(open + 1, close).toInt();
    start = close + 1;
  }
  return 0;
}

int getArmIndex(String target) {
  if (target.startsWith("ARM")) {
    int num = target.substring(3).toInt();
    if (num >= 1 && num <= 5) return num - 1;
  }
  return -1;
}

void sendConfirm(String target, String request) {
  String response = "!!" + target + ":[MASTER]:CONFIRM:" + request + "##";
  Serial.println(response);
}

void sendStarArrived(String target) {
  String response = "!!" + target + ":[MASTER]:REQUEST:STAR_ARRIVED##";
  Serial.println(response);
}

void checkArmsTimers() {
  unsigned long now = millis();
  for (int i = 0; i < 5; i++) {
    if (!arms[i].active) continue;

    // Send star if max brightness reached
    if (arms[i].brightness >= MAX_BRIGHTNESS) {
      sendStarArrived("ARM" + String(i + 1));
      arms[i].active = false;
      arms[i].brightness = 0;
      continue;
    }

    // Send star if idle for 2s
    if ((now - arms[i].lastPeakTime) > IDLE_TIMEOUT_MS) {
      sendStarArrived("ARM" + String(i + 1));
      arms[i].active = false;
      arms[i].brightness = 0;
      continue;
    }

    // Send star if 10s elapsed since MAKE_STAR
    if ((now - arms[i].startTime) > SEND_DELAY_MS) {
      sendStarArrived("ARM" + String(i + 1));
      arms[i].active = false;
      arms[i].brightness = 0;
    }
  }
}
