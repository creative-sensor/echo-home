/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package msg;

import java.io.CharArrayReader;
import java.io.IOException;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.xml.parsers.ParserConfigurationException;
import javax.xml.parsers.SAXParserFactory;
import org.xml.sax.InputSource;
import org.xml.sax.SAXException;
import ses.TimeStamp;
import xml.BSSXML;

/**
 *
 * @author Creativ
 */
public class BSSMessage {

    int id;
    String content;
    TimeStamp ts;
    

    public BSSMessage() {
    }

    public BSSMessage(String data) {
        try {

            SAXParserFactory factory = SAXParserFactory.newInstance();
            javax.xml.parsers.SAXParser saxparser = factory.newSAXParser();
            BSSXML bssxml = new BSSXML();

            saxparser.parse(new InputSource(new CharArrayReader(data.toCharArray())), bssxml);
            this.content = bssxml.getParsedContent();
            this.ts = bssxml.getParsedTimeStamp();
            this.id = bssxml.getParsedID();

        } catch (IOException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);

        } catch (ParserConfigurationException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);
        } catch (SAXException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);
            System.out.print(ex.getMessage());
        }
    }

    @Override
    public String toString() {
        String s = "<bssmessage>"
                + "<id>" + this.id + "</id>"
                + "<content>" + this.content + "</content>"
                + this.ts.toString();// timestamp in XML string

        return s += "</bssmessage>\n";
    }

    public String getContent() {
        return content;
    }

    public TimeStamp getTimeStamp() {
        return this.ts;
    }

    public int SenderID() {
        return this.id;
    }

    public void setContent(String data) {
        this.content = data;
    }

    public void setTimeStamp(TimeStamp ts) {
        this.ts = ts;
    }

    public void setSenderID(int id) {
        this.id = id;
    }
}
