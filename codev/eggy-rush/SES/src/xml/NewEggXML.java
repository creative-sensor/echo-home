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
public class NewEggXML extends DefaultHandler{
     String ROW = "row";
    String COL = "col";
    
    boolean hitROW = false;
    boolean hitCOL = false;
    
    int parsed_row;
    int parsed_col;

    @Override
    public void startElement(String uri, String localName, String qName, Attributes attributes) throws SAXException {
        if (ROW.equalsIgnoreCase(qName)) {
            hitROW = true;
        }

        if (COL.equalsIgnoreCase(qName)) {
            hitCOL = true;
        }       
    }

    @Override
    public void characters(char[] ch, int start, int length) throws SAXException {
        String value = new String(ch, start, length);
        if (hitROW) {
            this.parsed_row = Integer.parseInt(value);
            this.hitROW = false;
        }

        if (hitCOL) {
            this.parsed_col = Integer.parseInt(value);
            this.hitCOL = false;
        }
    }
    
    public int getParsedRow(){
        return this.parsed_row;
    }
    
    public int getParsedCol(){
        return this.parsed_col;
    }

}
