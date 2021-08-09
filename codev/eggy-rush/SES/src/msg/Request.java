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
import xml.RequestXML;

/**
 *
 * @author Creativ
 */
public class Request {

    private int cs; // id of requested CS
    private int id; // id of source that requests
    private int sn; // sequence number;

    public Request(int cs, int id, int seqnum) {
        this.cs = cs;
        this.id = id;
        this.sn = seqnum;
    }

    public Request(String data) {
        try {

            SAXParserFactory factory = SAXParserFactory.newInstance();
            javax.xml.parsers.SAXParser saxparser = factory.newSAXParser();
            RequestXML rxml = new RequestXML();
            saxparser.parse(new InputSource(new CharArrayReader(data.toCharArray())), rxml);
            this.cs = rxml.getCSID();
            this.id = rxml.getID();
            this.sn = rxml.getSN();

        } catch (IOException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);

        } catch (ParserConfigurationException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);
        } catch (SAXException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);
            System.out.print(ex.getMessage());
        }
    }

    public int CSID() {
        return this.cs;
    }

    public int ID() {
        return this.id;
    }

    public int SequenceNum() {
        return this.sn;
    }

    @Override
    public String toString() {
        String xml = "<request>";
        xml += "<csid>" + cs + "</csid>";
        xml += "<id>" + Integer.toString(id) + "</id>";
        xml += "<sn>" + Integer.toString(sn) + "</sn>";
        xml += "</request>\n";
        return xml;
    }
}
