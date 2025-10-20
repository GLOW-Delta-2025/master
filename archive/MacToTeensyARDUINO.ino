// =====================================================
// Teensy / Arduino Firmware v4 (parallel delayed commands)
// Handles any [SOURCE]:REQUEST:[COMMAND]{params} format
// Delayed responses: STAR_ARRIVED & CLIMAX_READY
// Multiple delayed commands can run in parallel
// =====================================================

#include <Arduino.h>

String inputBuffer = "";
bool messageStarted = false;
const int LED_PIN = LED_BUILTIN;

struct DelayedCommand {
  String source;
  String command;              // STAR_ARRIVED or CLIMAX_READY
  unsigned long triggerTime;   // millis() when it should be sent
  bool active;
};

const int MAX_DELAYED = 10;
DelayedCommand delayed[MAX_DELAYED];

// =====================================================
// Helper function to schedule delayed commands
// =====================================================
void scheduleDelayedCommand(String source, String command) {
  for (int i = 0; i < MAX_DELAYED; i++) {
    if (!delayed[i].active) {
      delayed[i].source = source;
      delayed[i].command = command;
      delayed[i].triggerTime = millis() + random(5000, 8000);  // 5–8 seconds
      delayed[i].active = true;
      return;
    }
  }

  // Overflow protection
  Serial.println("!!" + source + ":MASTER:REQUEST:ERROR:{too_many_delayed_commands}##");
}

// =====================================================
// Setup
// =====================================================
void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  randomSeed(analogRead(0));

  // Initialize delayed commands array
  for (int i = 0; i < MAX_DELAYED; i++) delayed[i].active = false;
}

// =====================================================
// Main loop
// =====================================================
void loop() {
  // --- Handle incoming serial data ---
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

      if (inputBuffer.length() > 256) {
        Serial.println("!!UNKNOWN:MASTER:REQUEST:ERROR:{overflow}##");
        inputBuffer = "";
        messageStarted = false;
      }
    }
  }

  // --- Handle delayed commands ---
  unsigned long now = millis();
  for (int i = 0; i < MAX_DELAYED; i++) {
    if (delayed[i].active && now >= delayed[i].triggerTime) {
      sendConfirm(delayed[i].source, delayed[i].command);
      delayed[i].active = false;  // deactivate
    }
  }
}

// =====================================================
// Handle and parse full message (2-colon version)
// =====================================================
void handleMessage(String msg) {
  msg.trim();

  if (!msg.startsWith("!!") || !msg.endsWith("##")) {
    Serial.println("!!UNKNOWN:MASTER:REQUEST:ERROR:{invalid_format}##");
    return;
  }

  msg.remove(0, 2);
  msg.remove(msg.length() - 2, 2);

  int c1 = msg.indexOf(':');
  int c2 = msg.indexOf(':', c1 + 1);

  if (c1 < 0 || c2 < 0) {
    Serial.println("!!UNKNOWN:MASTER:REQUEST:ERROR:{bad_structure}##");
    return;
  }

  String source = msg.substring(0, c1);
  if (source.startsWith("!")) source.remove(0, 1);

  String maybeType = msg.substring(c1 + 1, c2);
  String command = msg.substring(c2 + 1);

  int bracePos = command.indexOf('{');
  if (bracePos >= 0) command = command.substring(0, bracePos);
  command.trim();

  blinkLED();

  if (maybeType.equalsIgnoreCase("REQUEST")) {
    String devices[] = {"ARM1", "ARM2", "ARM3", "ARM4", "ARM5", "CENTER", "TOP"};

    // Schedule delayed commands if needed
    if (command.equalsIgnoreCase("SEND_STAR")) {
      if (source.equalsIgnoreCase("BROADCAST")) {
        for (int i = 0; i < 7; i++) scheduleDelayedCommand(devices[i], "STAR_ARRIVED");
      } else {
        scheduleDelayedCommand(source, "STAR_ARRIVED");
      }
    }

    // Always send immediate confirm
    if (source.equalsIgnoreCase("BROADCAST")) {
      for (int i = 0; i < 7; i++) sendConfirm(devices[i], command);
    } else {
      sendConfirm(source, command);
    }
  } else {
    Serial.println("!!" + source + ":MASTER:REQUEST:ERROR:{unknown_type_" + maybeType + "}##");
  }
}

// =====================================================
// Visual blink feedback
// =====================================================
void blinkLED() {
  digitalWrite(LED_PIN, HIGH);
  delay(1000);
  digitalWrite(LED_PIN, LOW);
}

// =====================================================
// Generate proper CONFIRM messages including SOURCE
// =====================================================
void sendConfirm(String source, String command) {
  String msg = "!!" + source + ":MASTER:CONFIRM:";

  if (command.equalsIgnoreCase("MAKE_STAR")) {
    msg += "MAKE_STAR##";
  } else if (command.equalsIgnoreCase("SEND_STAR")) {
    msg += "SEND_STAR##";
  } else if (command.equalsIgnoreCase("CANCEL_STAR")) {
    msg += "CANCEL_STAR##";
  } else if (command.equalsIgnoreCase("STAR_ARRIVED")) {
    msg = "!!" + source + ":MASTER:REQUEST:";
    msg += "STAR_ARRIVED{";
    msg += "SPEED=" + String(random(10, 100)) + ",";
    msg += "COLOR=" + String(random(0, 255)) + ",";
    msg += "BRIGHTNESS=" + String(random(50, 255)) + ",";
    msg += "SIZE=" + String(random(1, 10)) + "}##";
  } else if (command.equalsIgnoreCase("ADD_STAR")) {
    msg += "ADD_STAR##";
  } else if (command.equalsIgnoreCase("BUILDUP_CLIMAX_CENTER")) {
    msg += "BUILDUP_CLIMAX_CENTER{SPEED=" + String(random(50, 200)) + "}##";
  } else if (command.equalsIgnoreCase("CLIMAX_READY")) {
    msg += "CLIMAX_READY##";
  } else if (command.equalsIgnoreCase("START_CLIMAX_CENTER")) {
    msg += "START_CLIMAX_CENTER{TIME=" + String(random(1000, 5000)) + "}##";
  } else if (command.equalsIgnoreCase("START_CLIMAX_TOP")) {
    msg += "START_CLIMAX_TOP{TIME=" + String(random(1000, 5000)) + "}##";
  } else if (command.equalsIgnoreCase("STOP_CLIMAX_CENTER")) {
    msg += "STOP_CLIMAX_CENTER##";
  } else if (command.equalsIgnoreCase("STOP_CLIMAX_TOP")) {
    msg += "STOP_CLIMAX_TOP##";
  } else if (command.equalsIgnoreCase("START_IDLE")) {
    msg += "START_IDLE##";
  } else if (command.equalsIgnoreCase("STOP_IDLE")) {
    msg += "STOP_IDLE##";
  } else if (command.equalsIgnoreCase("PING")) {
    msg += "PING##";
  } else if (command.equalsIgnoreCase("RESET")) {
    msg += "RESET##";
  } else if (command.equalsIgnoreCase("COMM_ERROR")) {
    msg += "COMM_ERROR{STRING=Random_Comm_Error_" + String(random(100, 999)) + "}##";
  } else {
    msg = "!!" + source + ":MASTER:REQUEST:ERROR:{unknown_command_" + command + "}##";
  }

  Serial.println(msg);
}
