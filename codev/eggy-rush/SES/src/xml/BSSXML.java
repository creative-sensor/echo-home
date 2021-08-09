/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package xml;

import org.xml.sax.Attributes;
import org.xml.sax.SAXException;
import org.xml.sax.helpers.DefaultHandler;
import ses.TimeStamp;

/**
 *
 * @author Creativ
 */
public class BSSXML extends DefaultHandler {

    String CONTENT = "content";
    String TIMESTAMP = "timestamp";
    String ID = "id";
    boolean hitCONTENT = false;
    boolean hitTIMESTAMP = false;
    boolean hitID = false;
    String parsed_content = null;
    TimeStamp parsed_ts = null;
    int parsedID;

    @Override
    public void startElement(String uri, String localName, String qName, Attributes attributes) throws SAXException {
        if (CONTENT.equalsIgnoreCase(qName)) {
            hitCONTENT = true;
        }

        if (TIMESTAMP.equalsIgnoreCase(qName)) {
            hitTIMESTAMP = true;
        }

        if (ID.equalsIgnoreCase(qName)) {
            hitID = true;
        }
    }

    @Override
    public void characters(char[] ch, int start, int length) throws SAXException {
        String value = new String(ch, start, length);
        if (hitCONTENT) {
            this.parsed_content = value;
            this.hitCONTENT = false;
        }

        if (hitTIMESTAMP) {
            TimeStamp ts = new TimeStamp(value);
            this.parsed_ts = ts;
            this.hitTIMESTAMP = false;
        }
        
        if(hitID){
            this.parsedID = Integer.parseInt(value);
            this.hitID = false;
        }
    }

    public String getParsedContent() {
        return this.parsed_content;
    }

    public TimeStamp getParsedTimeStamp() {
        return this.parsed_ts;
    }
    
    public int getParsedID(){
        return this.parsedID;
    }
}
