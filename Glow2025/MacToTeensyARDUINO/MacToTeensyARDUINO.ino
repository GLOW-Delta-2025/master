// ==========================================
// Teensy / Arduino Communication Firmware
// Blink LED for 1s when a valid command is received
// Send structured confirmation back to Mac
// ==========================================

String inputBuffer = "";
bool messageStarted = false;
const int LED_PIN = LED_BUILTIN;

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
}

void loop() {
  // Read incoming serial characters
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

      // Safety: avoid buffer overflow
      if (inputBuffer.length() > 256) {
        inputBuffer = "";
        messageStarted = false;
        Serial.println("!!MAC:REQUEST:ERROR:{overflow}##");
      }
    }
  }
}

// ------------------------------------------
// Handle message, blink LED, send confirmation
// ------------------------------------------
void handleMessage(String msg) {
  msg.trim();

  if (!msg.startsWith("!!") || !msg.endsWith("##")) {
    Serial.println("!!MAC:REQUEST:ERROR:{invalid_format}##");
    return;
  }

  // Strip !! and ##
  msg.remove(0, 2);
  msg.remove(msg.length() - 2, 2);

  // Split message: DEVICE:TYPE:COMMAND:{INFO}
  int firstColon = msg.indexOf(':');
  int secondColon = msg.indexOf(':', firstColon + 1);
  int thirdColon = msg.indexOf(':', secondColon + 1);

  if (firstColon < 0 || secondColon < 0 || thirdColon < 0) {
    Serial.println("!!MAC:REQUEST:ERROR:{bad_structure}##");
    return;
  }

  String device = msg.substring(0, firstColon);
  String type = msg.substring(firstColon + 1, secondColon);
  String command = msg.substring(secondColon + 1, thirdColon);
  String info = msg.substring(thirdColon + 1);

  info.replace("{", "");
  info.replace("}", "");

  // Blink LED for 1 second to confirm receipt
  digitalWrite(LED_PIN, HIGH);
  delay(1000);
  digitalWrite(LED_PIN, LOW);

  // Send structured confirmation back to Mac
  if (type.equalsIgnoreCase("REQUEST")) {
    String confirmMsg = "!!MASTER:CONFIRM:" + command + "{" + info + "}##";
    Serial.println(confirmMsg);
  } 
  else {
    Serial.println("!!MAC:REQUEST:ERROR:{unknown_type}##");
  }
}
