/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package xml;

import org.xml.sax.Attributes;
import org.xml.sax.SAXException;
import org.xml.sax.helpers.DefaultHandler;
import ses.Pair;
import ses.PairVector;
import ses.TimeStamp;

/**
 *
 * @author Creativ
 */
public class SESXML extends DefaultHandler {
    String CONTENT = "content";
    String TIMESTAMP = "timestamp";
    String VM = "vm";
    String PID = "pid";
    String PAIR = "pair";
    
    

    boolean hitCONTENT = false;
    boolean hitTIMESTAMP = false;
    boolean hitVM = false;
    boolean hitPID = false;
    boolean hitPAIR = false;
    
    String parsed_content = null;
    TimeStamp parsed_ts = null;
    PairVector parsed_VP = null;
    Pair currentPair = null;
    

    @Override
    public void startElement(String uri, String localName, String qName, Attributes attributes) throws SAXException {
        if (CONTENT.equalsIgnoreCase(qName)) {
            hitCONTENT = true;
        }

        if (TIMESTAMP.equalsIgnoreCase(qName)) {
            hitTIMESTAMP = true;
        }

        if (VM.equalsIgnoreCase(qName)) {
            hitVM = true;
        }
        if(PAIR.equalsIgnoreCase(qName)){
            hitPAIR = true;
        }
        if(PID.equalsIgnoreCase(qName)){
            hitPID = true;
        }
    }

    @Override
    public void characters(char[] ch, int start, int length) throws SAXException {
        String value = new String(ch,start,length);
        if (hitCONTENT) {
            this.parsed_content = value;
            this.hitCONTENT = false;
        }

        if (hitTIMESTAMP) {
            TimeStamp ts = new TimeStamp(value);
            if(hitPAIR){
                this.currentPair.setTime(ts.value());
                hitPAIR = false;
                this.parsed_VP.add(currentPair);
            }else{
                this.parsed_ts = ts;
            }
            
            this.hitTIMESTAMP = false;
        }
        if (hitVM) {
            this.parsed_VP = new PairVector();
            this.hitVM = false;
        }
        if(hitPAIR){
            this.currentPair = new Pair();
        }
        
        if(hitPID){
            //if(hitPAIR){ // hitPAIR is certain
                this.currentPair.setID(Integer.parseInt(value));
            //}            
            this.hitPID = false;
        }
    }
    
    public String getParsedContent(){
        return this.parsed_content;
    }
    
    public TimeStamp getParsedTimeStamp(){
        return this.parsed_ts;
    }
    
    public PairVector getParsedPairVector(){
        return this.parsed_VP;
    }
}
