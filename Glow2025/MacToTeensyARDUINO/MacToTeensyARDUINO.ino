// =====================================================
// Teensy / Arduino Firmware v3
// Protocol Implementation (6.2.1.1 / 6.2.1.2)
// Handles any MASTER:[SRC]:REQUEST:COMMAND{params} format
// Responds with correct CONFIRM or error
// Generates random values where parameters are expected
// =====================================================
 
String inputBuffer = "";
bool messageStarted = false;
const int LED_PIN = LED_BUILTIN;
 
void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  randomSeed(analogRead(0));
}
 
void loop() {
  while (Serial.available() > 0) {
    char c = Serial.read();
 
    // Start of message detection
    if (c == '!' && !messageStarted) {
      messageStarted = true;
      inputBuffer = "!!";
    }
    else if (messageStarted) {
      inputBuffer += c;
 
      // End of message detection
      if (inputBuffer.endsWith("##")) {
        handleMessage(inputBuffer);
        inputBuffer = "";
        messageStarted = false;
      }
 
      // Overflow protection
      if (inputBuffer.length() > 256) {
        Serial.println("!!MAC:REQUEST:ERROR:{overflow}##");
        inputBuffer = "";
        messageStarted = false;
      }
    }
  }
}
 
// =====================================================
// Handle and parse full message
// =====================================================
void handleMessage(String msg) {
  msg.trim();
 
  if (!msg.startsWith("!!") || !msg.endsWith("##")) {
    Serial.println("!!MAC:REQUEST:ERROR:{invalid_format}##");
    return;
  }
 
  // Strip wrappers
  msg.remove(0, 2);
  msg.remove(msg.length() - 2, 2);
 
  // Example formats:
  // MASTER:ARM1:REQUEST:MAKE_STAR{SPEED=100}
  // MASTER:BROADCAST:REQUEST:RESET##
  // MASTER:CENTER:REQUEST:BUILDUP_CLIMAX_CENTER{SPEED=80}
 
  // Split parts by colon
  int c1 = msg.indexOf(':');
  int c2 = msg.indexOf(':', c1 + 1);
  int c3 = msg.indexOf(':', c2 + 1);
  int c4 = msg.indexOf(':', c3 + 1);
 
  if (c1 < 0 || c2 < 0 || c3 < 0) {
    Serial.println("!!MAC:REQUEST:ERROR:{bad_structure}##");
    return;
  }
 
  String header1 = msg.substring(0, c1);               // MASTER
  String source  = msg.substring(c1 + 1, c2);          // ARM1 / CENTER / BROADCAST
  String maybeType = msg.substring(c2 + 1, c3);        // usually REQUEST
  String rest;
 
  if (c4 > 0) rest = msg.substring(c3 + 1);           // e.g. MAKE_STAR{...}
  else rest = msg.substring(c3 + 1);
 
  // Detect structure dynamically
  String type;
  String command;
 
  if (maybeType.equalsIgnoreCase("REQUEST")) {
    type = "REQUEST";
    command = rest;
  }
  else {
    // if structure has one extra colon, try next part
    int nextColon = rest.indexOf(':');
    if (nextColon > 0) {
      type = rest.substring(0, nextColon);
      command = rest.substring(nextColon + 1);
    } else {
      type = maybeType;
      command = rest;
    }
  }
 
  // Extract command before { if exists
  int bracePos = command.indexOf('{');
  if (bracePos > 0) command = command.substring(0, bracePos);
  command.trim();
 
  blinkLED();
 
  if (type.equalsIgnoreCase("REQUEST")) {
    sendConfirm(command);
  } else {
    Serial.println("!!MAC:REQUEST:ERROR:{unknown_type_" + type + "}##");
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
// Generate proper CONFIRM messages (6.2.1.2 definitions)
// =====================================================
void sendConfirm(String command) {
  String msg = "!!MASTER:CONFIRM:";
 
  if (command.equalsIgnoreCase("MAKE_STAR")) {
    msg += "MAKE_STAR##";
  }
  else if (command.equalsIgnoreCase("SEND_STAR")) {
    msg += "SEND_STAR##";
  }
  else if (command.equalsIgnoreCase("CANCEL_STAR")) {
    msg += "CANCEL_STAR##";
  }
  else if (command.equalsIgnoreCase("STAR_ARRIVED")) {
    msg += "STAR_ARRIVED{";
    msg += "SPEED=" + String(random(10, 100)) + ",";
    msg += "COLOR=" + String(random(0, 255)) + ",";
    msg += "BRIGHTNESS=" + String(random(50, 255)) + ",";
    msg += "SIZE=" + String(random(1, 10)) + "}##";
  }
  else if (command.equalsIgnoreCase("ADD_STAR")) {
    msg += "ADD_STAR##";
  }
  else if (command.equalsIgnoreCase("BUILDUP_CLIMAX_CENTER")) {
    msg += "BUILDUP_CLIMAX_CENTER{SPEED=" + String(random(50, 200)) + "}##";
  }
  else if (command.equalsIgnoreCase("CLIMAX_READY")) {
    msg += "CLIMAX_READY##";
  }
  else if (command.equalsIgnoreCase("START_CLIMAX_CENTER")) {
    msg += "START_CLIMAX_CENTER{TIME=" + String(random(1000, 5000)) + "}##";
  }
  else if (command.equalsIgnoreCase("START_CLIMAX_TOP")) {
    msg += "START_CLIMAX_TOP{TIME=" + String(random(1000, 5000)) + "}##";
  }
  else if (command.equalsIgnoreCase("STOP_CLIMAX_CENTER")) {
    msg += "STOP_CLIMAX_CENTER##";
  }
  else if (command.equalsIgnoreCase("STOP_CLIMAX_TOP")) {
    msg += "STOP_CLIMAX_TOP##";
  }
  else if (command.equalsIgnoreCase("START_IDLE")) {
    msg += "START_IDLE##";
  }
  else if (command.equalsIgnoreCase("STOP_IDLE")) {
    msg += "STOP_IDLE##";
  }
  else if (command.equalsIgnoreCase("PING")) {
    msg += "PING##";
  }
  else if (command.equalsIgnoreCase("RESET")) {
    msg += "RESET##";
  }
  else if (command.equalsIgnoreCase("COMM_ERROR")) {
    msg += "COMM_ERROR{STRING=Random_Comm_Error_" + String(random(100, 999)) + "}##";
  }
  else {
    msg = "!!MAC:REQUEST:ERROR:{unknown_command_" + command + "}##";
  }
 
  Serial.println(msg);
}
 