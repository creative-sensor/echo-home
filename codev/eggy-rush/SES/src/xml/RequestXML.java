/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package xml;

import org.xml.sax.Attributes;
import org.xml.sax.SAXException;
import org.xml.sax.helpers.DefaultHandler;

/**
 *
 * @author Creativ
 */
public class RequestXML extends DefaultHandler {

    int cs;
    int id;
    int sn;
    final String CSID = "csid";
    final String ID = "id";
    final String SN = "sn";
    boolean hitCSID = false;
    boolean hitID = false;
    boolean hitSN = false;

    @Override
    public void startElement(String uri, String localName, String qName, Attributes attributes) throws SAXException {
        if (CSID.equalsIgnoreCase(qName)) {
            hitCSID = true;
        }
        if (ID.equalsIgnoreCase(qName)) {
            hitID = true;
        }
        if (SN.equalsIgnoreCase(qName)) {
            hitSN = true;
        }
    }

    @Override
    public void characters(char[] ch, int start, int length) throws SAXException {
        String value = new String(ch, start, length);
        if (hitCSID) {
            this.cs = Integer.parseInt(value);
            this.hitCSID = false;
        }
        if (hitID) {
            this.id = Integer.parseInt(value);
            this.hitID = false;
        }
        if (hitSN) {
            this.sn = Integer.parseInt(value);
            this.hitSN = false;
        }

    }
    
    public int getCSID(){
        return this.cs;
    }
    
    public int getID(){
        return this.id;
    }
    
    public int getSN(){
        return this.sn;
    }
}
