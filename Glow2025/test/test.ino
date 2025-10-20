/*
  Glow 25' Teensy Simulator (non-blocking)
  Simulates ARM1–ARM5 and sends STAR_ARRIVED/CLIMAX_READY.
*/

#define SERIAL_BAUD 115200
String inputBuffer = "";

struct ArmState {
  int brightness;
  bool active;
  bool waitingSend;
  unsigned long sendStart;
  String target;
};
ArmState arms[5];

void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial);
  Serial.println("Glow25 Teensy Simulator Ready (non-blocking)");
  for (int i=0;i<5;i++){arms[i].brightness=0;arms[i].active=false;arms[i].waitingSend=false;}
}

void loop() {
  readSerialMessages();
  checkPendingSends();
}

void readSerialMessages() {
  while (Serial.available()) {
    char c = Serial.read();
    inputBuffer += c;
    if (inputBuffer.endsWith("##")) {
      handleMessage(inputBuffer);
      inputBuffer = "";
    }
  }
}

void handleMessage(String msg) {
  msg.trim();
  if (!msg.startsWith("!!")) return;
  int s = msg.indexOf('[');
  int e = msg.indexOf(']');
  if (s==-1||e==-1) return;
  String target = msg.substring(s+1,e);
  String req = extractRequest(msg);
  sendConfirm(target, req);

  if (req=="MAKE_STAR") {
    setBrightness(target, 50);
  } else if (req=="UPDATE_STAR") {
    setBrightness(target, extractBrightness(msg));
  } else if (req=="SEND_STAR") {
    int i=getArmIndex(target);
    if (i>=0){arms[i].waitingSend=true;arms[i].sendStart=millis();arms[i].target=target;}
  } else if (req=="BUILDUP_CLIMAX_CENTER"||req=="BUILDUP_CLIMAX_TOP") {
    delay(50);
    sendClimaxReady(target);
  }
}

void checkPendingSends() {
  unsigned long now = millis();
  for (int i=0;i<5;i++){
    if (arms[i].waitingSend && now-arms[i].sendStart>1200){
      sendStarArrived(arms[i].target);
      arms[i].waitingSend=false;
      arms[i].active=false;
      arms[i].brightness=0;
    }
  }
}

String extractRequest(String msg){
  int p1=msg.indexOf(':',msg.indexOf(':')+1);
  if (p1==-1) return "";
  String part=msg.substring(p1+1);
  part.replace("##","");
  int b=part.indexOf('{');if(b!=-1)part=part.substring(0,b);
  part.trim();
  return part;
}

int extractBrightness(String msg){
  int first=msg.indexOf('['), last=msg.lastIndexOf(']');
  if(first==-1||last==-1)return 0;
  String params=msg.substring(first,last+1);
  int cnt=0,start=0;
  while(true){
    int o=params.indexOf('[',start);
    if(o==-1)break;
    int c=params.indexOf(']',o);
    if(c==-1)break;
    cnt++;
    if(cnt==3)return params.substring(o+1,c).toInt();
    start=c+1;
  }
  return 0;
}

int getArmIndex(String t){
  if(!t.startsWith("ARM"))return -1;
  int n=t.substring(3).toInt();
  if(n>=1&&n<=5)return n-1;
  return -1;
}

void setBrightness(String target,int b){
  int i=getArmIndex(target);
  if(i>=0){arms[i].brightness=b;arms[i].active=true;}
}

void sendConfirm(String target,String req){
  Serial.print("!!");Serial.print(target);
  Serial.print(":[MASTER]:CONFIRM:");Serial.print(req);Serial.println("##");
}

void sendStarArrived(String target){
  Serial.print("!!");Serial.print(target);
  Serial.println(":[MASTER]:REQUEST:STAR_ARRIVED##");
}

void sendClimaxReady(String target){
  Serial.print("!!");Serial.print(target);
  Serial.println(":[MASTER]:REQUEST:CLIMAX_READY##");
}
