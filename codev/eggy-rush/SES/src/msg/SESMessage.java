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
import ses.PairVector;
import ses.TimeStamp;
import xml.SESXML;

/**
 *
 * @author Creativ
 */
public class SESMessage{
    String content;
    TimeStamp ts;
    PairVector VM;
    
    
    public String getContent() {
        return content;
    }
     
    public TimeStamp getTimeStamp(){
        return this.ts;
    }
    
    public PairVector getVM(){
        return this.VM;
    }

    
    public String toLog() {
        String formatted = "\r\n#MESSAGE:\r\n";
        formatted += "\t" + this.content + "\r\n";
        formatted += this.ts.toLog();
        formatted += this.VM.toLog();
        return formatted;
    }
    
    public String toString() {
        String s =  "<sesmessage>" +
                "<content>" + this.content + "</content>" +
                this.ts.toString();// timestamp in XML string
        if(this.VM != null){
            // VM vector in XML string
            s += this.VM.toString();
        }
        return s += "</sesmessage>\n";
    }
    
    public void setContent(String data){
        this.content = data;
    }
    
    public void setTimeStamp(TimeStamp ts){
        this.ts = ts;
    }
    
    public void setVM(PairVector VP){
        this.VM = VP;
    }

    public SESMessage() {
    }
    
    public SESMessage(String data) {
        try {

            SAXParserFactory factory = SAXParserFactory.newInstance();
            javax.xml.parsers.SAXParser saxparser = factory.newSAXParser();
            SESXML sesxml = new SESXML();

            saxparser.parse(new InputSource(new CharArrayReader(data.toCharArray())), sesxml);
            this.content = sesxml.getParsedContent();
            this.ts = sesxml.getParsedTimeStamp();
            this.VM = sesxml.getParsedPairVector();
            
        } catch (IOException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);

        } catch (ParserConfigurationException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);
        } catch (SAXException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);
            System.out.print(ex.getMessage());
        }
    }
}
